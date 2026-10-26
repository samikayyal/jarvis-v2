"""Bounded real-model evaluations with no real tool execution or external writes."""

import asyncio
import json
from pathlib import Path
import sys

from jarvis_personal_runtime.config import load_runtime_config
from jarvis_personal_runtime.responses import DirectResponsesRunner, OpenAIRawResponsesAdapter
from jarvis_personal_runtime.runtime import PersonalRuntime, InboundText
from datetime import UTC, datetime


async def main():
    release = Path(sys.argv[1])
    loaded = load_runtime_config(
        Path('/home/samik/Projects/Jarvis/other/runtime'),
        config_path='/home/samik/Projects/Jarvis/other/config/jarvis.toml',
    )
    prompt = (release / 'deployment/personal-runtime/SYSTEM.md.example').read_text()
    adapter = OpenAIRawResponsesAdapter.from_api_key(loaded.secrets.openai_api_key)
    cases = [
        ('identity', 'Who are you, which model are you using, and where do you run? Keep it brief.'),
        ('hosting', 'I am considering moving you from my Ubuntu laptop to GCP. Without researching prices yet, explain what would move and what would stay the same. Do I need a GPU or a new model provider?'),
        ('incomplete_evidence', 'Evaluate this hypothetical research result: the laptop has enough measured headroom; Google search was blocked; the official VM page explained billing but did not expose a price for the selected machine; a Vertex AI page showed Gemini token pricing. Give a short useful recommendation and explain what is still missing. Do not invent prices or claim to have performed these checks yourself.'),
    ]
    try:
        for index, (name, question) in enumerate(cases):
            runner = DirectResponsesRunner(adapter, request_timeout_seconds=90)
            runtime = PersonalRuntime(loaded.config, request_runner=runner, system_prompt=prompt)
            result = await runtime.receive(InboundText(str(index), question, datetime.now(UTC)))
            print(json.dumps({'case': name, 'disposition': result.disposition.value, 'replies': result.replies}), flush=True)
    finally:
        await adapter.close()


asyncio.run(main())
