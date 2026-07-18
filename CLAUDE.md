# THE ROOM — project notes for Claude

## Record immutability (hard rule)

The public ledger and the channel are a track record. Its credibility depends on
never being quietly edited. Therefore:

- **Published posts and cards are never re-uploaded or edited in place.** Once a
  message or image is in the channel, it stays as posted.
- **Factual errors** (score, outcome, resolved level/close, a wrong resolution)
  are corrected only by a **separate correction post** that links the fixing
  commit in the public ledger (e.g. the revert SHA). Never silently overwrite.
- **Cosmetic changes** (wording, label formats, layout) take effect from the
  **next issue** only. Retroactively re-rendering or re-posting old cards is
  forbidden — even when the new format is nicer.
- A caption that is *factually correct* needs no fix even if later reworded. E.g.
  the old bet card "Resolves Jul 19, 00:00 UTC" names the same instant as the
  newer "Resolves at Jul 18 close" (the Jul 18 daily candle prints at Jul 19
  00:00 UTC) — correct, so it is left as posted.

Corrections are posted via the `announce` workflow (`.github/workflows/announce.yml`,
`gh workflow run announce.yml -f message="..."`) so the bot token never leaves
GitHub Actions secrets.

## Resolution model (single source of truth)

A pair created on UTC day D with a 24h horizon settles on **day D+1's** Binance
daily close (a 168h weekly on D+7). That candle is labelled by its open date and
prints at 00:00 UTC the next day. `resolve_close_date()` in `main.py` is the one
source both the resolver and the bet card derive from. Invariant: one daily close
settles exactly one pair — if two same-horizon pairs map to the same close, the
resolver refuses both and alerts the owner (never double-count). Regression tests:
`python tests.py`.

Do not reintroduce a "24h" horizon label in any user-facing text — a ~00:20 post
settles ~39h later on the next daily close, so "24h" is misleading and conflicts
with the resolve date. State the resolve date instead.
