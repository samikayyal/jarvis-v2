Type: task
Status: unclaimed
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
