# Official Google pricing page probe

Date: 2026-11-02 (Asia/Amman)

This is an independent public HTTP probe run with `uv`; it is evidence about the
current Google documents, not a replay of Jarvis's Selenium worker. The extraction
method is `urllib.request` followed by BeautifulSoup `html.parser` and
`get_text(' ', strip=True)`. It therefore differs from Jarvis's worker, which
loads Chrome, reads `document.body.innerText`, and returns only an 8,000-character
prefix.

## Overview URL and navigation target

Command:

```powershell
uv run python -X utf8 -c "import urllib.request; from bs4 import BeautifulSoup; requested='https://cloud.google.com/products/compute/pricing'; resp=urllib.request.urlopen(requested,timeout=60); soup=BeautifulSoup(resp.read(),'html.parser'); target='https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types'; print([(a.get_text(' ',strip=True),a.get('href')) for a in soup.find_all('a',href=True) if a.get('href')==target])"
```

Observed navigation output (the link appears twice in the page HTML):

```text
[("E2", "https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types"),
 ("E2", "https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types")]
```

The overview request returned HTTP 200 at
`https://cloud.google.com/products/compute/pricing`. Its visible-text equivalent
was 57,000 characters in this probe, but neither `e2-standard-2` nor `e2-medium`
occurred in that extracted text (`find()` returned `-1` for both). This is a
probe result, not a claim about every browser rendering of the overview.

## Direct general-purpose page

Command:

```powershell
uv run python -X utf8 -c "import urllib.request; from bs4 import BeautifulSoup; requested='https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types'; resp=urllib.request.urlopen(requested,timeout=60); raw=resp.read(); soup=BeautifulSoup(raw,'html.parser'); text=soup.get_text(' ',strip=True); i=text.lower().find('e2-standard-2'); j=text.lower().find('e2-medium'); print('requested_url=',requested); print('final_url=',resp.geturl()); print('status=',resp.status); print('raw_bytes=',len(raw)); print('extraction=urllib.request + BeautifulSoup(html.parser).get_text(space,strip=True)'); print('text_chars=',len(text)); print('e2_standard_2_offset=',i); print('e2_medium_offset=',j); print('e2_standard_2_snippet=',repr(text[i-140:i+330])); print('e2_medium_snippet=',repr(text[j-140:j+330]))"
```

Observed output:

```text
requested_url= https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types
final_url= https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types
status= 200
raw_bytes= 35814052
extraction=urllib.request + BeautifulSoup(html.parser).get_text(space,strip=True)
text_chars= 207212
e2_standard_2_offset= 90218
e2_medium_offset= 137275
e2_standard_2_snippet= ... e2-standard-2 2 8 GiB $0.06701142 / 1 hour $0.048248226 / 1 hour $0.036186166 / 1 hour $0.042217196 / 1 hour $0.030155144 / 1 hour ...
e2_medium_snippet= ... e2-medium 2 4 GiB $0.03350571 / 1 hour $0.024124113 / 1 hour $0.018093083 / 1 hour $0.021108598 / 1 hour $0.015077572 / 1 hour ...
```

The first hourly value in each row is the default column shown by the page; the
remaining values are discount columns. The excerpt is deliberately not treated
as a complete monthly quote because region selection, disk, networking, and
discount assumptions still need to be stated. The large offsets and the 207,212
character extracted document explain why Jarvis's 8,000-character body prefix of
the overview could not contain these rows.

## Interpretation boundary

The probe confirms that the official general-purpose page is a direct, readable
source for the E2 rows at this time. It does **not** prove that Jarvis could have
returned the same text: Jarvis's extraction is browser-visible text, applies
different DOM/script behavior, and enforces a per-page prefix. The incident trace
itself remains the authoritative evidence for what Jarvis actually saw.
