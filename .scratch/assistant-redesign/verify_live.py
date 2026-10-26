"""Read only the latest acceptance request; print allowlisted deployment facts."""

import json
from pathlib import Path

path = Path('/home/samik/Projects/Jarvis/other/runtime/data/runtime-trace.jsonl')
matched = None
for line in path.open():
    event = json.loads(line)
    if event['event'] != 'responses_raw_exchange':
        continue
    request = json.loads(event['payload']['request_body'])
    inputs = json.dumps(request.get('input', []))
    if 'Answer briefly from your current runtime context' in inputs:
        matched = (event, request)
if matched is None:
    raise SystemExit('Acceptance request not present yet')
event, request = matched
instructions = request.get('instructions', '')
response = json.loads(event['payload']['response_body'])
print(json.dumps({
    'timestamp': event['timestamp'],
    'http_status': event['payload']['status_code'],
    'model': request['model'],
    'tools_exposed': [tool.get('name') for tool in request.get('tools', [])],
    'identity_guidance': 'You are Jarvis' in instructions,
    'runtime_context': '## Runtime context' in instructions,
    'active_model_context': '"active_model": "gpt-6-luna"' in instructions,
    'limit_context': '"max_tool_calls_per_request": 64' in instructions,
    'hosting_baseline': 'hold the configured model provider constant' in instructions,
    'response_output_types': [item['type'] for item in response.get('output', [])],
}))
