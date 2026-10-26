"""Activate a validated release and prompt together, with automatic rollback."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

base = Path('/home/samik/Projects/Jarvis')
revision = sys.argv[1]
assert len(revision) == 40 and all(c in '0123456789abcdef' for c in revision)
current = base / 'code/current'
previous = current.resolve()
assert previous.name == '5dfac1eebb68d32f7a0f60ea1b23616bc1a93662'
release = base / 'code/releases' / revision
prompt = base / 'other/runtime/SYSTEM.md'
config = (base / 'other/config/jarvis.toml').resolve()
config_bytes = config.read_bytes()
prompt_bytes = prompt.read_bytes()
candidate_prompt = (release / 'deployment/personal-runtime/SYSTEM.md.example').read_bytes()
backup = base / 'other/backups' / ('assistant-redesign-' + revision)
backup.mkdir(parents=True, exist_ok=False)
(backup / 'SYSTEM.md').write_bytes(prompt_bytes)
(backup / 'release.txt').write_text(str(previous))


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)


def point(target):
    pending = current.with_name('current.assistant-pending')
    assert not pending.exists() and not pending.is_symlink()
    pending.symlink_to(target)
    pending.replace(current)


def validate(target):
    run('sudo', '-n', '-u', 'jarvis-personal-runtime',
        str(target / '.venv/bin/jarvis-personal-runtime'), '--root',
        str(base / 'other/runtime'), '--config', str(config), '--check')


validate(release)
run('sudo', '-n', 'systemctl', 'stop', 'jarvis-personal-runtime')
try:
    assert config.read_bytes() == config_bytes
    assert prompt.read_bytes() == prompt_bytes
    prompt.write_bytes(candidate_prompt)
    validate(release)
    point(release)
    run('sudo', '-n', 'systemctl', 'start', 'jarvis-personal-runtime')
    time.sleep(3)
    run('systemctl', 'is-active', '--quiet', 'jarvis-personal-runtime')
    ready = run('sudo', '-n', '-u', 'jarvis-personal-runtime', '/usr/local/bin/uv',
        'run', '--no-project', '--python', str(release / '.venv/bin/python'),
        'python', str(base / 'code/web-readiness.py'))
    assert config.read_bytes() == config_bytes
    assert prompt.read_bytes() == candidate_prompt
    print(ready.stdout)
except BaseException:
    run('sudo', '-n', 'systemctl', 'stop', 'jarvis-personal-runtime')
    prompt.write_bytes(prompt_bytes)
    point(previous)
    run('sudo', '-n', 'systemctl', 'start', 'jarvis-personal-runtime')
    print('ROLLED BACK', file=sys.stderr)
    raise
print(json.dumps({'active_release': str(current.resolve()), 'previous_release': str(previous),
    'prompt_backup': str(backup / 'SYSTEM.md'), 'config_unchanged': True,
    'prompt_sha256': hashlib.sha256(candidate_prompt).hexdigest()}))
