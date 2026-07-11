# THE ROOM

An autonomous daily Telegram digest for Bitcoin. Two AI analysts with opposing
methodologies break down one market signal of the day, lock in mutually
exclusive, verifiable predictions and keep a public accuracy score. Everything
runs on GitHub Actions on a cron — no servers.

## How it works

- **🔮 Oracle** — macro analyst: liquidity regimes, contrarian signals, fear
  extremes as opportunities.
- **🛡 Guardian** — risk analyst: flow inertia, base rates, demand for
  confirmation.

Each day (08:20 UTC+8 = 00:20 UTC) the run:

1. Resolves yesterday's predictions against the BTCUSDT daily close on Binance
   and posts the resolution with the score.
2. Collects news (MarketTwits) and market data (prices, funding, open interest,
   Fear & Greed) — all sources free, no keys.
3. The classifier (Claude Haiku) synthesizes 2–4 related headlines into the
   day's configuration; if nothing qualifies, a fallback issue is produced
   (a myth, a rematch of an old argument, or a positioning breakdown).
4. The debate engine (Claude Sonnet) generates the post: layered signal header,
   four stance-first debate lines, a regime read, and two mutually exclusive
   predictions against one watershed level (24h horizon, confidence 0.55–0.80).
5. Renders a PNG card and posts card → text → the "Who's right tomorrow?" poll
   to the Telegram channel, appends predictions to `ledger.csv` and commits it.
6. On Sundays (UTC+8) it also posts the week's scorecard.

## How to verify the ledger

`ledger.csv` is the single source of truth; each row is one prediction:

| Field | Meaning |
|---|---|
| `direction`, `level` | "above/below the level" at expiry |
| `expires_utc` | when the 24h horizon ends |
| `price_at_expiry` | the first **daily close** of BTCUSDT on Binance after `expires_utc` |
| `result` | `pending` / `win` / `loss` |
| `brier` | `(confidence − outcome)²`, where outcome = 1 on a win, 0 on a loss |

To check any row by hand: pull the BTCUSDT daily candles
(`GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d`), find
the first close after `expires_utc` and compare it to `level`. The full history
lives in the git log: every write and every resolution is a separate bot commit,
nothing is edited after the fact.

## Running it

Repository secrets (Settings → Secrets and variables → Actions):

- `ANTHROPIC_API_KEY` — Anthropic API key;
- `TG_BOT_TOKEN` — Telegram bot token;
- `TG_CHANNEL_ID` — production channel id (e.g. `@the_room_btc` or `-100…`);
- `TG_CHANNEL_ID_STAGING` — private staging channel id.

Schedule: `.github/workflows/daily.yml` (cron `20 0 * * *` = 00:20 UTC). Manual
runs use `workflow_dispatch`.

### Run modes

Behaviour is selected by env vars (precedence: `DRY_RUN` > `FORCE` > `STAGE` >
production):

| Mode | Channel | Ledger | When |
|---|---|---|---|
| production | `TG_CHANNEL_ID` | `ledger.csv` | scheduled cron, or manual with **force = true** |
| `STAGE=1` | `TG_CHANNEL_ID_STAGING` | `ledger_staging.csv` (git-ignored) | manual `workflow_dispatch` default |
| `DRY_RUN=1` | none — printed to stdout | `ledger_dry.csv` (git-ignored) | local debugging |

Staging and dry runs bypass the once-per-day dedup, so they can be rerun freely.

### Local debugging (DRY_RUN)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
DRY_RUN=1 python main.py
```

In this mode messages are printed to stdout instead of being sent to Telegram,
and the ledger is written to `ledger_dry.csv` (not committed).

## Success criteria

The Day-30 kill test is pre-registered in [`thresholds.md`](thresholds.md):
demand (subscribers, poll participation, view retention) decides go/no-go;
accuracy and Brier from `ledger.csv` only explain the result.

## Disclaimer

Not financial advice. The predictions are a public experiment in calibrating AI
analysts; the entire track record is open in `ledger.csv`.
