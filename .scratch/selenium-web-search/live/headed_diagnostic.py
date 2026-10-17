"""One-query headed diagnostic using the installed runtime and private Xvfb."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from jarvis_personal_runtime.web_browser import _minimal_environment
from jarvis_personal_runtime.web_worker import SeleniumPageReader
from jarvis_personal_runtime.web_search import extract_google_results

if len(sys.argv) == 1:
    with tempfile.TemporaryDirectory(prefix="jarvis-headed-diagnostic-") as folder:
        env = _minimal_environment()
        env["HOME"] = folder
        process = subprocess.Popen(
            [
                "/usr/bin/xvfb-run",
                "-a",
                "-s",
                "-screen 0 1280x900x24",
                sys.executable,
                __file__,
                folder,
            ],
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = process.communicate(timeout=60)
            print(out, end="")
            if process.returncode:
                print(
                    json.dumps(
                        {"diagnostic_exit": process.returncode, "error": err[-1500:]}
                    )
                )
        except subprocess.TimeoutExpired:
            print(json.dumps({"diagnostic_error": "outer timeout"}))
        finally:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                time.sleep(0.2)
            process.communicate(timeout=5)
            members = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(fields[2]) == process.pid and fields[0] != "Z":
                        members.append(entry.name)
                except (OSError, ValueError):
                    pass
    print(
        json.dumps(
            {
                "temporary_directory_removed": not Path(folder).exists(),
                "remaining_process_group_members": members,
            }
        )
    )
    raise SystemExit(0)

from selenium import webdriver

report = {
    "mode": "headed Chrome on isolated Xvfb",
    "uid": os.getuid(),
    "query": "site:iana.org example domains",
    "headless_removed": False,
}


def headed_factory(*, service, options):
    assert "--headless=new" in options.arguments
    options.arguments.remove("--headless=new")
    assert not any(arg.startswith("--headless") for arg in options.arguments)
    assert (
        "--enable-sandbox" in options.arguments
        and "--no-sandbox" not in options.arguments
    )
    report["headless_removed"] = True
    return webdriver.Chrome(service=service, options=options)


reader = SeleniumPageReader(
    Path(sys.argv[1]) / "profile", 20, 8000, driver_factory=headed_factory
)
try:
    reader._start()
    reader.driver.get("chrome://sandbox")
    report["sandbox_status"] = reader.driver.find_element("tag name", "body").text
    started = time.monotonic()
    result = reader.read(
        "https://www.google.com/search?q=site%3Aiana.org+example+domains&num=5",
        search=True,
    )
    report.update(
        read_outcome=result["outcome"],
        reason=result.get("reason"),
        operation_seconds=round(time.monotonic() - started, 2),
        proxy_blocked=reader.proxy.blocked,
        proxy_reason=reader.proxy.blocked_reason,
    )
    if reader.driver is not None:
        current = urlsplit(reader.driver.current_url)
        report["final_origin_path"] = (
            f"{current.scheme}://{current.netloc}{current.path}"
        )
        body = reader.driver.find_element("tag name", "body").text
        body = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<IP redacted>", body)
        body = re.sub(r"https?://\S+", "<URL omitted>", body)
        report["visible_text"] = body[:1500]
        report["has_recaptcha"] = bool(
            reader.driver.find_elements(
                "css selector", '.g-recaptcha, #captcha, iframe[src*="recaptcha"]'
            )
        )
        parsed = extract_google_results(reader.driver.page_source, 5, 8000)
        report["parser_outcome"] = parsed["outcome"]
        report["result_count"] = len(parsed.get("results", []))
        report["results"] = [
            {"title": r["title"], "url": r["url"]} for r in parsed.get("results", [])
        ]
except Exception as exc:
    report["diagnostic_error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
finally:
    reader.close()
print(json.dumps(report, indent=2))
