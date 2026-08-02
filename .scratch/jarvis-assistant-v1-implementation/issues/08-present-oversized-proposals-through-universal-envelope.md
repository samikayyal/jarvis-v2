# 08 — Present oversized proposals through the universal envelope

**What to build:** Any oversized mutation proposal is presented as one ordered, bounded, digest-bound fragment sequence, and approval is impossible until the complete proposal has been delivered unambiguously.

**Blocked by:** 07 — Freeze and execute one exact approval-gated action.

**Status:** ready-for-agent

- [ ] Every fragment carries the same action identity and digest and remains within the messaging size bound.
- [ ] The confirmation prompt becomes valid only after every ordered fragment has been accepted by the outbound adapter.
- [ ] Failed, incomplete, reordered, or ambiguous presentation invalidates the entire proposal without selective automatic retry.

