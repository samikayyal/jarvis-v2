Type: task
Status: complete
Blocked by: 02

## Question

Implement the smallest production runtime change that loads the configured
remote Google MCP service, validates its checked-in operation manifest, and
adds `/connections`, `/connect google`, and `/disconnect google`; bounded
Gmail, Drive, and Calendar reads; and exact one-attempt Gmail send/reply and
Calendar create/update approvals.

Expose no generic discovered operations or credentials, keep `2` terminal-only,
invalidate pending Google writes after connection changes, avoid automatic
retry of ambiguous writes, and leave later remote services addable through the
same small configuration, manifest, and prepared-tool seam.

## Answer

Implemented the production configured-MCP seam for the current official hosted
Google contract. `jarvis.toml` now accepts explicit remote service records and
checked-in manifests; `.env` alone carries the OAuth client and refresh-token
material. `--check` validates every manifest locally, while normal startup uses
the stateless Streamable HTTP lifecycle to fail closed against exact selected
operation digests before any prepared tool is exposed.

The runtime adds `/connections`, `/connect google`, and `/disconnect google`,
binds one in-memory Google connection across all configured services, refreshes
short-lived access tokens, and invalidates pending writes whenever that
connection is replaced or disconnected. OAuth values and authorization headers
do not enter model context or runtime traces.

The checked-in manifests expose 12 bounded tools: Gmail search/thread/message
reads; Drive search, metadata, text read, and allowlisted text export; Calendar
search/list/read plus exact create/update writes. Extra discovered operations
remain unavailable. Calendar writes freeze the complete narrowed arguments,
accept only `1`, `9`, or `/cancel`, execute once, and never retry an ambiguous
outcome; terminal-only `2` behavior remains unchanged.

The official hosted Gmail endpoint captured and reverified on 2026-09-02 still
exposes no send or reply operation. Consequently Gmail send/reply is not
implemented: substituting direct Gmail API calls, an unofficial MCP service, or
mislabeling `create_draft` would violate the resolved boundary. Gmail remains
read-only until an official selected operation exists and its manifest is
reviewed.

Focused evidence before final review: 12 live prepared operations verified;
`167 passed, 1 skipped` in `tests/personal_runtime`; Ruff check/format and
Python compilation passed.

## Comments

- Completed on 2026-09-02 for the supportable official Google MCP contract;
  Gmail send/reply remains an explicit upstream blocker, not a hidden fallback.
