# Bug: Multi-pick messages silently skip second pick when first resolves earlier

## Summary

When a single Telegram message contains multiple non-parlay picks (e.g. Laformula: Dodgers -1.5 + Pistons 1H -1.5), and the games finish in different tracker run windows, the second pick is never graded or broadcast.

## Root Cause

In `tracker.py`, the message loop skips any message that already contains a verdict emoji:

```python
if any(ch in text for ch in VERDICT_EMOJI.values()):
    continue  # already graded
```

So after run 1 edits in ✅ for the first pick, run 2 sees an emoji and skips the entire message — even though the second pick is still unresolved.

## Reproduction

1. Create a message with two non-parlay picks whose games finish hours apart
2. Wait for the first game to finish → tracker grades it, edits ✅ into message
3. Wait for the second game to finish → tracker skips the message (already has emoji)
4. Second pick never gets ✅/❌ and never gets broadcast

## Impact

- Silent: no error logged, no audit entry for the skipped pick
- Broadcast: only the first pick fires; second pick is never broadcast
- DB: only one audit row for the message (first grading only)

## Relevant Code

- **Skip logic:** `tracker.py` message loop — `if any(ch in text for ch in VERDICT_EMOJI.values()): continue`
- **Overall verdict:** `_overall_verdict()` in `tracker.py` — returns PENDING if any pick is PENDING, which would prevent the edit... but only if they're in the same run
- **Parse cache:** `parse_cache.json` — used for PENDING picks, but a partially-graded message (already has emoji) is skipped before cache is consulted

## Proposed Fix Direction

Instead of skipping messages with ANY emoji, count resolved vs total picks:

1. Parse the message (using cache if available)
2. Count how many picks are in it
3. Count how many already have an emoji in the text
4. If all picks accounted for → skip
5. If some picks still missing emoji → continue grading only the unresolved ones

This requires being smarter about "is this message fully graded?" — probably by comparing parsed pick count to emoji count in the text, or by checking the parse cache for partially-resolved messages.

Alternative simpler approach: always re-parse and re-grade all picks in a message, but skip editing legs that already have an emoji. The overall edit would only fire when all legs are resolved.

## Files to Touch

- `tracker.py` — skip logic, grading loop, emoji insertion
- `audit.py` — `broadcast_results()` already handles partial verdicts correctly (filters to resolved only)
- `parse_cache.json` / `_load_pending_cache()` — may need to store partial grading state
