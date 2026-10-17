"""Bounded anonymous browser experiment; never interacts with challenges.

Run with the installed runtime Python through uv. Offline mode uses an owned
data-document fixture and blocks HTTP(S); search mode uses the unchanged reader.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from jarvis_personal_runtime.web_browser import _minimal_environment
from jarvis_personal_runtime.web_search import extract_google_results
from jarvis_personal_runtime.web_worker import SeleniumPageReader


def members(group):
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == group and fields[0] != "Z":
                    found.append(int(entry.name))
            except (OSError, ValueError):
                pass
    return found


def redact(text):
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<IP redacted>", text)
    return re.sub(r"https?://\S+", "<URL omitted>", text)[:1500]


def modify(options, variant):
    if "--headless=new" in options.arguments and not variant.startswith("headless"):
        options.arguments.remove("--headless=new")
    options.add_argument("--window-size=1280,900")
    if variant in {"minimal", "headless-minimal"}:
        options.add_argument("--disable-blink-features=AutomationControlled")
    assert "--no-sandbox" not in options.arguments
    assert "--enable-sandbox" in options.arguments


PROBE = """() => {
 const c=document.createElement('canvas');
 const gl=c.getContext('webgl');
 const ext=gl && gl.getExtension('WEBGL_debug_renderer_info');
 return {webdriver:navigator.webdriver, userAgent:navigator.userAgent,
 platform:navigator.platform, languages:navigator.languages,
 brands:navigator.userAgentData ? navigator.userAgentData.toJSON() : null,
 plugins:Array.from(navigator.plugins,p=>p.name),
 webgl:ext ? [gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),
 gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)] : null,
 chromeRuntime:!!(window.chrome && window.chrome.runtime),
 outer:[outerWidth,outerHeight],inner:[innerWidth,innerHeight]};
}"""


def offline(args):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    options = Options()
    options.binary_location = "/usr/bin/google-chrome-stable"
    for arg in (
        "--enable-sandbox",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-gpu",
        "--proxy-server=http://127.0.0.1:9",
        "--proxy-bypass-list=<-loopback>",
        f"--user-data-dir={args.home}/profile",
    ):
        options.add_argument(arg)
    modify(options, args.variant)
    driver = webdriver.Chrome(
        service=Service("/usr/local/bin/chromedriver"), options=options
    )
    try:
        driver.set_page_load_timeout(20)
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs", {"urls": ["http://*", "https://*"]}
        )
        driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "deny"})
        driver.get("chrome://sandbox")
        sandbox = driver.find_element("tag name", "body").text
        assert "Yes" in sandbox and "Seccomp-BPF" in sandbox, sandbox
        fixture = (
            "<html><title>Owned offline fixture</title><script>window.observed=("
            + PROBE
            + ")();</script><body>fixture</body></html>"
        )
        driver.get("data:text/html;charset=utf-8," + quote(fixture))
        observed = driver.execute_script("return window.observed")
        assert observed is not None
        assert observed["webdriver"] == (args.variant == "stock"), observed
        return {
            "mode": "offline",
            "variant": args.variant,
            "sandbox": sandbox,
            "observed": observed,
            "expected_webdriver_verified": True,
        }
    finally:
        driver.quit()


def search(args):
    from selenium import webdriver

    def factory(*, service, options):
        modify(options, args.variant)
        return webdriver.Chrome(service=service, options=options)

    reader = SeleniumPageReader(
        Path(args.home) / "profile", 20, 8000, driver_factory=factory
    )
    report = {"mode": "search", "variant": args.variant}
    try:
        started = time.monotonic()
        result = reader.search("site:iana.org example domains", 5)
        report.update(
            result=result,
            seconds=round(time.monotonic() - started, 2),
            proxy_blocked=reader.proxy.blocked,
        )
        if reader.driver is not None:
            descriptor, snapshot = tempfile.mkstemp(
                prefix="jarvis-search-evidence-", suffix=".html"
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(reader.driver.page_source)
            report["snapshot"] = snapshot
            report["blocking_elements"] = [
                {
                    "html": element.get_attribute("outerHTML")[:1200],
                    "displayed": element.is_displayed(),
                }
                for element in reader.driver.find_elements(
                    "css selector",
                    '#captcha, .g-recaptcha, #challenge-form, [role="dialog"][aria-modal="true"], form[action*="consent.google.com"]',
                )
            ]
            current = urlsplit(reader.driver.current_url)
            report["final_origin_path"] = (
                f"{current.scheme}://{current.netloc}{current.path}"
            )
            report["visible_text"] = redact(
                reader.driver.find_element("tag name", "body").text
            )
            report["has_recaptcha"] = bool(
                reader.driver.find_elements(
                    "css selector", '.g-recaptcha, #captcha, iframe[src*="recaptcha"]'
                )
            )
            report["parsed"] = extract_google_results(
                reader.driver.page_source, 5, 8000
            )
        return report
    finally:
        reader.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "search"], required=True)
    parser.add_argument(
        "--variant",
        choices=["stock", "minimal", "headless", "headless-minimal"],
        required=True,
    )
    parser.add_argument("--home")
    args = parser.parse_args()
    if args.home:
        try:
            result = offline(args) if args.mode == "offline" else search(args)
            print(json.dumps(result))
            return 0
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__, "detail": redact(str(exc))}))
            return 1
    with tempfile.TemporaryDirectory(prefix="jarvis-stealth-diagnostic-") as folder:
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
                "--mode",
                args.mode,
                "--variant",
                args.variant,
                "--home",
                folder,
            ],
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        code = 1
        try:
            out, err = process.communicate(timeout=55)
            print(out, end="")
            code = process.returncode
            if code:
                print(json.dumps({"stderr": redact(err)}))
        except subprocess.TimeoutExpired:
            print(json.dumps({"error": "outer timeout"}))
        finally:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                time.sleep(0.2)
            process.communicate(timeout=5)
            remaining = members(process.pid)
    print(
        json.dumps(
            {
                "temporary_directory_removed": not Path(folder).exists(),
                "remaining_process_group_members": remaining,
            }
        )
    )
    return code or bool(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
