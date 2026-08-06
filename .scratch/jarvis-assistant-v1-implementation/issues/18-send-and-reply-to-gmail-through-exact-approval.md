# 18 — Send and reply to Gmail through exact approval

**What to build:** Gmail new-send and typed-reply proposals freeze the complete externally meaningful email operation and dispatch it once only after exact approval.

**Blocked by:** 08 — Present oversized proposals through the universal envelope; 17 — Read bounded Gmail, Calendar, and Drive content.

**Status:** complete

- [ ] New-send proposals freeze all recipients, subject, body, MIME, and other delivery-affecting fields in the exact preview.
- [ ] Reply proposals additionally freeze the source message, source thread, reply headers, and threading behavior.
- [ ] Altered, expired, replayed, mismatched-thread, and outcome-unknown cases dispatch no replacement message and perform no automatic retry.

