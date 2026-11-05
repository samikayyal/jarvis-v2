import asyncio,json
from pathlib import Path
from jarvis_personal_runtime.config import WebSearchConfig
from jarvis_personal_runtime.web_tools import WebTools
rows=json.loads(Path('.scratch/pricing-research-diagnosis/web-trace.json').read_text(encoding='utf-8-sig'))
class Replay:
 def __init__(self):
  self.searches=[json.loads(e['payload']['output']) for e in rows if e['event']=='tool_result' and e['payload']['name']=='search_web']
  self.pages=[p for e in rows if e['event']=='tool_result' and e['payload']['name']=='read_web_pages' for p in json.loads(e['payload']['output'])['pages']]
 async def search(self,query,limit): return self.searches.pop(0)
 async def read(self,url,**kw): return self.pages.pop(0)
 async def close(self): pass
async def main():
 b=Replay();t=WebTools(WebSearchConfig(enabled=True),runtime_dir=Path('.scratch/pricing-research-diagnosis'),max_output_chars=65536,browser_factory=lambda *a:b)
 pages=[]
 for e in rows:
  if e['event']!='tool_call':continue
  p=e['payload'];out=json.loads(await t.execute(p['name'],p['arguments']));pages+=out.get('pages',[])
  print(p['name'],out['outcome'],out['remaining'])
 await t.close_request()
 found=any('e2-standard-2' in p['text'].lower() or 'e2-medium' in p['text'].lower() for p in pages)
 print('Historical tool-sequence replay: E2 rate-row evidence present =',found)
 print('Duplicate canonical overview =',pages[0]['url']==pages[-1]['url'],'identical excerpt =',pages[0]['text']==pages[-1]['text'])
 assert found, 'FAIL: all search/page attempts spent without either requested E2 machine row in page evidence'
asyncio.run(main())
