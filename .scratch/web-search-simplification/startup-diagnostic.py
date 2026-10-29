"""Compare initial static navigation with active and candidate worker sources."""

import asyncio
import http.server
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from jarvis_personal_runtime.web_browser import BrowserWorker


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<html><title>Pricing fixture</title><body><h1>VM prices</h1><div id="prices">Loading pricing</div><script>setTimeout(() => {document.getElementById("prices").innerText="Ready";},350);</script></body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


async def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for label, source in [
            ("active", "/home/samik/Projects/Jarvis/code/current/src"),
            ("candidate", str(Path.cwd() / "src")),
        ]:
            with tempfile.TemporaryDirectory(
                prefix="jarvis-startup-diagnostic-"
            ) as directory:
                root = Path(directory)
                script = root / "fixture_worker.py"
                script.write_text(
                    f"import sys\nsys.path.insert(0, {source!r})\n"
                    "from jarvis_personal_runtime import web_worker as w\n"
                    "original=w.resolve_public_addresses\n"
                    'w.resolve_public_addresses=lambda host,port: ("127.0.0.1",) if host=="fixture.example" else original(host,port)\n'
                    'w.find_chromium=lambda: "/usr/bin/google-chrome"\n'
                    'w.find_chromedriver=lambda: "/usr/local/bin/chromedriver"\n'
                    "raise SystemExit(w.main())\n"
                )

                async def factory(*argv, script=script, **kwargs):
                    return await asyncio.create_subprocess_exec(
                        *argv[: argv.index(sys.executable)],
                        sys.executable,
                        str(script),
                        *argv[argv.index("--profile-dir") :],
                        **kwargs,
                    )

                worker = BrowserWorker(
                    root / "profiles", 60, 8000, process_factory=factory, headed=True
                )
                start = time.monotonic()
                try:
                    result = await worker.read(
                        f"http://fixture.example:{server.server_port}/dynamic#prices"
                    )
                    print(
                        json.dumps(
                            {
                                "source": label,
                                "seconds": time.monotonic() - start,
                                "result": result,
                            }
                        ),
                        flush=True,
                    )
                finally:
                    await worker.close()
    finally:
        server.shutdown()
        server.server_close()


asyncio.run(main())
