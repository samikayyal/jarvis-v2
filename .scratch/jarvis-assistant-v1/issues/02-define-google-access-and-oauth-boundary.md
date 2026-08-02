Type: research
Status: resolved
Blocked by:

## Question

What exact Gmail, Google Calendar, and Google Drive APIs, OAuth application type, consent flow, token-storage boundary, least-privilege scopes, revocation behavior, and tool-level action matrix satisfy the locked V1 read and approval-gated write capabilities for one Google account?

## Answer

Resolved in the [Google access and OAuth boundary research artifact](../research/google-access-and-oauth-boundary.md). It selects one server-side Web application authorization-code flow and one backend refresh-token record; uses fixed read scopes with incremental `gmail.send` and `calendar.events` write scopes; and keeps OAuth grants separate from Jarvis approval. Ticket 10 supersedes the research artifact's encryption-at-rest recommendation: the refresh token uses a plaintext `0600` file in a private `0700` directory owned only by the Google connector so it can rotate the token atomically. Gmail sends and Calendar event changes remain approval-gated, while Drive mutations and destructive Google actions remain excluded.

Ticket 10 also resolves the deployment boundary left open by the research: the
exact registered HTTPS URL is deployment configuration, but its path is the sole
public Jarvis endpoint and performs only the state-bound authorization-code
callback. Every Jarvis control, webhook, connector, state, audit, and worker
surface remains local or private-overlay-only.

## Comments
