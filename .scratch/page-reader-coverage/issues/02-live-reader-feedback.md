# Explain refreshed captures and quote validation to the reader

Status: ready-for-agent
Type: task

## Problem

Live acceptance on 2026-11-06 exposed two failures on deployed commit
`df91f127224c7c5789d6aabbeee15f656139849e`. Details, timings and reader IDs are
in [deployment verification](../deployment-verification.md).

1. A successful runtime refresh replaces the reader's history and fetches a new
   capture, but the reader is not explicitly told that the refresh has already
   succeeded. On both RFC 9110 and example.com, it interpreted the focused query's
   request to refresh as an unavailable action it must perform itself. It then
   claimed freshness was unverified, even after an additional rendered capture.
2. Invalid quotes are rejected in `_findings`, but `_answer` retains the raw
   proposed findings and enters fallback without giving the reader the specific
   validation error. The RFC reader repeatedly joined a heading and a distant
   paragraph into one quote, consumed fallback/context, and required a fresh
   reader to recover a valid contiguous sentence.

These observations identify model/runtime feedback gaps; they do not justify
relaxing exact-quote validation or overriding semantic uncertainty as success.
An additional presentation defect is malformed nested Markdown links in the
WhatsApp quote; retain this as a separate formatting concern.

## Proposed bounded correction

- Give the reader trusted per-invocation capture provenance: newly captured,
  successfully refreshed and replaced, or retained without fetching. Do not add
  timestamps, TTL tracking or another persistence layer.
- Feed quote-validation failures back explicitly and allow a bounded correction
  within the existing attempt/action/time/context limits before broader fallback.
  Preserve mandatory fallback and unsupported-answer suppression on failed repair.
- Keep failed-refresh preservation, same-reader identity, coverage disclosure,
  URL/browser controls and cancellation semantics unchanged.

## Acceptance

- Regression tests distinguish successful refresh, retained follow-up, and failed
  refresh; freshness is never asserted on the basis of old evidence.
- Invalid noncontiguous quote receives actionable feedback; a corrected contiguous
  quote can succeed without unrelated scanning, and repeated invalid quotes remain
  bounded and unsupported.
- Repeat visible RFC and short-page refresh acceptance plus trace inspection.
- Any runtime fix must be committed, pushed, GitHub-verified, tested and deployed
  through the existing isolated deployment branch before claiming it live.

## Comments

Filed after completing the authorized deployment and testing. No runtime correction
for these failures has been implemented or deployed in this acceptance pass.
