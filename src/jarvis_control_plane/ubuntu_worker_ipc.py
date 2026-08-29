"""Compatibility facade for the legacy ubuntu_worker_ipc module."""

from __future__ import annotations

from .application.compatibility import install_mirrors
from .workers import ipc_protocol as _ipc_protocol
from .workers.ipc_server import serve_ubuntu_worker_connection
from .workers.ipc_transport import (
    ReconnectingUnixSocketUbuntuWorkerTransport,
    UnixSocketUbuntuWorkerTransport,
)

_MAX_FRAME_BYTES = _ipc_protocol._MAX_FRAME_BYTES
_MAX_PENDING_MESSAGES = _ipc_protocol._MAX_PENDING_MESSAGES
_PollingFrameReceiver = _ipc_protocol._PollingFrameReceiver
_decode_frame_body = _ipc_protocol._decode_frame_body
_error_to_wire = _ipc_protocol._error_to_wire
_identity_from_wire = _ipc_protocol._identity_from_wire
_identity_to_wire = _ipc_protocol._identity_to_wire
_invocation_from_wire = _ipc_protocol._invocation_from_wire
_invocation_to_wire = _ipc_protocol._invocation_to_wire
_lifecycle_payload = _ipc_protocol._lifecycle_payload
_progress_from_wire = _ipc_protocol._progress_from_wire
_progress_to_wire = _ipc_protocol._progress_to_wire
_recv_exact = _ipc_protocol._recv_exact
_recv_frame = _ipc_protocol._recv_frame
_require_keys = _ipc_protocol._require_keys
_required_bool = _ipc_protocol._required_bool
_required_int = _ipc_protocol._required_int
_required_object = _ipc_protocol._required_object
_required_text = _ipc_protocol._required_text
_required_text_allow_empty = _ipc_protocol._required_text_allow_empty
_required_text_list = _ipc_protocol._required_text_list
_result_from_wire = _ipc_protocol._result_from_wire
_result_to_wire = _ipc_protocol._result_to_wire
_send_frame = _ipc_protocol._send_frame
_strict_object = _ipc_protocol._strict_object

__all__ = [
    "ReconnectingUnixSocketUbuntuWorkerTransport",
    "UnixSocketUbuntuWorkerTransport",
    "serve_ubuntu_worker_connection",
]

install_mirrors(
    __name__,
    {
        name: (_ipc_protocol,)
        for name in (
            "_MAX_FRAME_BYTES",
            "_MAX_PENDING_MESSAGES",
            "_PollingFrameReceiver",
            "_decode_frame_body",
            "_error_to_wire",
            "_identity_from_wire",
            "_identity_to_wire",
            "_invocation_from_wire",
            "_invocation_to_wire",
            "_lifecycle_payload",
            "_progress_from_wire",
            "_progress_to_wire",
            "_recv_exact",
            "_recv_frame",
            "_require_keys",
            "_required_bool",
            "_required_int",
            "_required_object",
            "_required_text",
            "_required_text_allow_empty",
            "_required_text_list",
            "_result_from_wire",
            "_result_to_wire",
            "_send_frame",
            "_strict_object",
        )
    },
)
