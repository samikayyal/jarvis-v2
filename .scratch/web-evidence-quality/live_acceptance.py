"""Sanitized deployment readiness and web acceptance evidence; never sends messages."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pwd
import subprocess
import sys

from jarvis_personal_runtime.config import load_runtime_config
from jarvis_personal_runtime.responses import build_direct_responses_runner

BASE = Path("/home/samik/Projects/Jarvis")
ARTIFACTS = BASE / "other/runtime/web-quality-acceptance"
ARTIFACTS.mkdir(exist_ok=True)
loaded = load_runtime_config(
    BASE / "other/runtime", config_path=BASE / "other/config/jarvis.toml"
)

if sys.argv[1] == "mark":
    stamp = datetime.now(timezone.utc).isoformat()
    (ARTIFACTS / "trace-start.json").write_text(json.dumps({"start": stamp}))
    print(json.dumps({"start": stamp}))
elif sys.argv[1] == "contract":
    runner = build_direct_responses_runner(
        "unused-no-model-request", loaded.config, config_path=loaded.toml_path
    )
    tools = runner._tools
    initial = {d["name"] for d in tools.definitions}
    assert "run_terminal" in initial
    asyncio.run(tools.execute("load_skill", {"name": "web"}))
    definition = next(d for d in tools.definitions if d["name"] == "read_web_pages")
    props = definition["parameters"]["properties"]
    assert props["render_mode"]["enum"] == ["static", "rendered"]
    assert "wait_for_text" in props
    assert 'render_mode="rendered"' in tools.instructions
    assert "For pricing comparisons" in tools.instructions
    assert "load relevant skills" in loaded.system_prompt
    print(
        json.dumps(
            {
                "terminal_immediately_exposed": True,
                "render_modes": props["render_mode"]["enum"],
                "wait_for_text_exposed": True,
                "pricing_guidance_preserved": True,
                "prompt_skill_guidance": True,
                "evidence_chars": loaded.config.web_search.max_total_evidence_chars,
            }
        )
    )
elif sys.argv[1] == "trace":
    start = json.loads((ARTIFACTS / "trace-start.json").read_text())["start"]
    records = []
    paths = sorted(
        loaded.config.trace_path.parent.glob(loaded.config.trace_path.name + "*")
    )
    for path in paths:
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("timestamp", "") < start:
                continue
            event, payload = row["event"], row["payload"]
            item = {"timestamp": row["timestamp"], "event": event}
            if event == "tool_call":
                name = payload.get("name")
                item["name"] = name
                if name in ("load_skill", "search_web", "read_web_pages"):
                    item["arguments"] = payload.get("arguments")
            elif event == "tool_result":
                item["name"] = payload.get("name")
                if item["name"] not in ("load_skill", "search_web", "read_web_pages"):
                    continue
                output = payload.get("output", "")
                try:
                    output = json.loads(output) if isinstance(output, str) else output
                except (ValueError, TypeError):
                    output = {}
                if isinstance(output, dict):
                    for key in (
                        "outcome",
                        "remaining",
                        "loaded",
                        "reason",
                        "stop_reason",
                    ):
                        if key in output:
                            item[key] = output[key]
                    for key in ("pages", "results"):
                        if key in output:
                            item[key] = [
                                {
                                    k: v
                                    for k, v in p.items()
                                    if k
                                    in (
                                        "title",
                                        "url",
                                        "requested_url",
                                        "outcome",
                                        "reason",
                                        "evidence_type",
                                        "truncated",
                                        "more_content_available",
                                        "source_chars",
                                        "extraction_method",
                                        "section_found",
                                        "render_mode",
                                        "render_wait",
                                        "stop_reason",
                                    )
                                }
                                | {"text_chars": len(p.get("text", ""))}
                                for p in output[key]
                            ]
            elif event in (
                "request_error",
                "request_cleanup_error",
                "request_timeout",
                "openwa_send_result",
                "openwa_send_error",
            ):
                item.update(
                    {
                        k: v
                        for k, v in payload.items()
                        if k
                        in ("disposition", "code", "may_have_sent", "timeout_seconds")
                    }
                )
            else:
                continue
            records.append(item)
    records.sort(key=lambda r: r["timestamp"])
    (ARTIFACTS / "trace-summary.json").write_text(json.dumps(records, indent=2))
    print(json.dumps(records, indent=2))
elif sys.argv[1] == "health":
    uid = pwd.getpwnam("jarvis-personal-runtime").pw_uid
    processes = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != uid:
                continue
            command = (
                (directory / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
            executable = (directory / "comm").read_text().strip()
            if (
                executable in ("chrome", "chromedriver", "chrome_crashpad", "Xvfb")
                or " -m jarvis_personal_runtime.web_worker " in command
            ):
                processes.append({"pid": directory.name, "executable": executable})
        except (OSError, ProcessLookupError):
            pass
    profiles = list((BASE / "other/runtime/data/web").glob(".jarvis-web-*"))
    print(
        json.dumps(
            {
                "active_release": str((BASE / "code/current").resolve()),
                "browser_processes": processes,
                "profile_count": len(profiles),
                "evidence_chars": loaded.config.web_search.max_total_evidence_chars,
                "prompt_sha256": hashlib.sha256(
                    (BASE / "other/runtime/SYSTEM.md").read_bytes()
                ).hexdigest(),
            }
        )
    )
    print(
        subprocess.run(
            [
                "systemctl",
                "show",
                "jarvis-personal-runtime",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "NRestarts",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert not processes and not profiles
