"""One bounded Google diagnostic; no challenge interaction or credentials."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from jarvis_personal_runtime.web_browser import _minimal_environment
from jarvis_personal_runtime.web_worker import SeleniumPageReader
from jarvis_personal_runtime.web_search import extract_google_results

if len(sys.argv) == 1:
    with tempfile.TemporaryDirectory(prefix="jarvis-google-diagnostic-") as folder:
        environment = _minimal_environment()
        environment["HOME"] = folder
        result = subprocess.run(
            [sys.executable, __file__, folder], env=environment, timeout=50
        )
    print(json.dumps({"temporary_directory_removed": not Path(folder).exists()}))
    raise SystemExit(result.returncode)

reader = SeleniumPageReader(Path(sys.argv[1]) / "profile", 20, 8000)
try:
    result = reader.read(
        "https://www.google.com/search?q=site%3Aiana.org+example+domains&num=5",
        search=True,
    )
    report = {
        "read_outcome": result["outcome"],
        "reason": result.get("reason"),
        "proxy_blocked": reader.proxy.blocked,
        "proxy_reason": reader.proxy.blocked_reason,
    }
    if reader.driver is not None:
        current = urlsplit(reader.driver.current_url)
        report["final_origin_path"] = (
            f"{current.scheme}://{current.netloc}{current.path}"
        )
        report["title"] = reader.driver.title
        body = reader.driver.find_element("tag name", "body").text
        body = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<IP redacted>", body)
        body = re.sub(r"https?://\S+", "<URL omitted>", body)
        report["visible_text"] = body[:1800]
        report["has_recaptcha"] = bool(
            reader.driver.find_elements(
                "css selector", '.g-recaptcha, #captcha, iframe[src*="recaptcha"]'
            )
        )
        report["parser_outcome"] = extract_google_results(
            reader.driver.page_source, 5, 8000
        )["outcome"]
    print(json.dumps(report, indent=2))
finally:
    reader.close()
