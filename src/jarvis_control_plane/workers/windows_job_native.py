"""Native Windows Job Object operations for the job executor."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from time import monotonic, sleep
from typing import BinaryIO

from ..ports import ActionDispatcherError


class _WindowsJobObjectNativeMixin:
    @staticmethod
    def _open_frozen_redirection_target(target: str) -> BinaryIO:
        """Open once while preventing reparse substitution, then write by handle."""

        import ctypes
        import msvcrt
        from ctypes import wintypes

        generic_write = 0x40000000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        open_existing = 3
        create_always = 2
        file_attribute_reparse_point = 0x00000400
        file_flag_backup_semantics = 0x02000000
        file_flag_open_reparse_point = 0x00200000
        invalid_handle_value = ctypes.c_void_p(-1).value
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        def open_handle(path: Path, *, directory: bool) -> int:
            handle = kernel32.CreateFileW(
                str(path),
                0 if directory else generic_write,
                file_share_read | file_share_write,
                None,
                open_existing if directory else create_always,
                file_flag_open_reparse_point
                | (file_flag_backup_semantics if directory else 0),
                None,
            )
            if int(handle) == invalid_handle_value:
                raise ctypes.WinError(ctypes.get_last_error())
            info = ByHandleFileInformation()
            if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise ctypes.WinError(error)
            if info.dwFileAttributes & file_attribute_reparse_point:
                kernel32.CloseHandle(handle)
                raise ActionDispatcherError(
                    "Windows redirection target changed through a reparse path"
                )
            return int(handle)

        candidate = Path(target)
        parent_handles: list[int] = []
        target_handle = 0
        try:
            for parent in reversed(candidate.parents):
                parent_handles.append(open_handle(parent, directory=True))
            target_handle = open_handle(candidate, directory=False)
            descriptor = msvcrt.open_osfhandle(target_handle, os.O_WRONLY | os.O_BINARY)
            target_handle = 0  # descriptor owns the native handle now
            return os.fdopen(descriptor, "wb", buffering=0)
        finally:
            if target_handle:
                kernel32.CloseHandle(wintypes.HANDLE(target_handle))
            for handle in reversed(parent_handles):
                kernel32.CloseHandle(wintypes.HANDLE(handle))

    def _create_job_object(self) -> int:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000008
        limits.BasicLimitInformation.ActiveProcessLimit = self.process_limit
        success = kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        if not success:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        return int(handle)

    @staticmethod
    def _assign_process(job_handle: int, process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        success = kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job_handle), wintypes.HANDLE(process._handle)
        )
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _resume_process(process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll")
        status = ntdll.NtResumeProcess(wintypes.HANDLE(process._handle))
        if status != 0:
            raise OSError(f"NtResumeProcess failed with status {status:#x}")

    @staticmethod
    def _terminate_unassigned_process(
        process: subprocess.Popen[bytes], timeout_seconds: int
    ) -> None:
        """Kill and reap a suspended child that never entered the Job Object."""

        try:
            process.kill()
            process.wait(timeout=timeout_seconds)
        except BaseException as exc:
            raise ActionDispatcherError(
                "unassigned suspended Windows child could not be stopped",
                may_have_dispatched=True,
            ) from exc
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    @staticmethod
    def _terminate_and_wait(job_handle: int, timeout_seconds: int) -> bool:
        import ctypes
        from ctypes import wintypes

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1):
            return False
        deadline = monotonic() + timeout_seconds
        while True:
            info = BasicAccountingInformation()
            returned = wintypes.DWORD()
            success = kernel32.QueryInformationJobObject(
                wintypes.HANDLE(job_handle),
                1,
                ctypes.byref(info),
                ctypes.sizeof(info),
                ctypes.byref(returned),
            )
            if not success:
                return False
            if info.ActiveProcesses == 0:
                return True
            if monotonic() >= deadline:
                return False
            sleep(0.01)

    @staticmethod
    def _close_handle(job_handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        if job_handle:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                wintypes.HANDLE(job_handle)
            )
