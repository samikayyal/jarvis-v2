"""Read-only verification of direct terminal exposure and the acceptance trace."""

import json
import sys
from importlib.resources import files
from pathlib import Path

from jarvis_personal_runtime.config import load_runtime_config
from jarvis_personal_runtime.skill_tools import SkillTools
from jarvis_personal_runtime.terminal import RunTerminalTool

base = Path('/home/samik/Projects/Jarvis')
loaded = load_runtime_config(base / 'other/runtime', config_path=base / 'other/config/jarvis.toml')
tools = SkillTools(RunTerminalTool)
assert [item['name'] for item in tools.definitions] == ['run_terminal']
assert not files('jarvis_personal_runtime').joinpath('skills/terminal/SKILL.md').is_file()
print(json.dumps({'terminal_exposed_immediately': True, 'terminal_skill_removed': True,
    'safe_probe_commands': [command for command in ('hostname', 'pwd', 'uname')
        if any(command == prefix or command.startswith(prefix + ' ')
            for prefix in loaded.config.ubuntu_read_only_prefixes)]}))

if '--trace' not in sys.argv:
    raise SystemExit(0)

exchanges = []
for line in loaded.config.trace_path.open():
    event = json.loads(line)
    if event['event'] != 'responses_raw_exchange':
        continue
    request = json.loads(event['payload']['request_body'])
    if 'Check direct terminal access' not in json.dumps(request.get('input', [])):
        continue
    response = json.loads(event['payload']['response_body'])
    loader = next((tool for tool in request.get('tools', []) if tool['name'] == 'load_skill'), None)
    exchanges.append({
        'timestamp': event['timestamp'], 'status': event['payload']['status_code'],
        'tools': [tool['name'] for tool in request.get('tools', [])],
        'skill_catalog': loader['parameters']['properties']['name']['enum'] if loader else [],
        'terminal_guidance': '## Terminal access' in request.get('instructions', ''),
        'calls': [{'name': item['name'], 'arguments': json.loads(item['arguments'])}
                  for item in response.get('output', []) if item['type'] == 'function_call'],
    })
assert exchanges, 'Live acceptance not present in trace'
assert all('run_terminal' in item['tools'] and 'terminal' not in item['skill_catalog']
           and item['terminal_guidance'] and item['status'] == 200 for item in exchanges)
calls = [call for item in exchanges for call in item['calls']]
assert calls == [{'name': 'run_terminal', 'arguments': {'host': 'ubuntu', 'command': 'pwd'}}], calls
print(json.dumps({'acceptance_exchanges': exchanges}))
