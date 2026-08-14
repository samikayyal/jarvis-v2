# Future OpenWA handoff

OpenWA remains independently deployed and independently operated. The future two-member private handoff network contains only OpenWA and `inbound_receiver`.
It must not be created or attached by this bundle. OpenWA's SSRF guard stays
enabled; its persistent Compose candidate must set `SSRF_ALLOWED_HOSTS` to the
single internal hostname `inbound_receiver` before the controlled recreation.

During a later supervised activation, an administrator may create that internal,
non-published network and attach the two reviewed members in one controlled
OpenWA recreation. That action requires the complete OpenWA verification ladder:
deployment validation, container health, named-session readiness, exact inbound
and outbound text, retained pairing after controlled recreation, stop-grace,
unchanged LAN/firewall exposure, and settled resource checks. A logout stops the
exercise without repeated recreation or re-pairing.

After both reviewed members are attached, run
`python -m jarvis_control_plane.openwa_webhook` as root from the pinned release.
It reads the API key and HMAC signing secret from their mode-0600 credential
documents, creates or updates exactly one `message.received` webhook to
`http://inbound_receiver:9011/webhook`, and fails closed on a duplicate or
conflicting subscriber. It prints only a sanitized action and count.
