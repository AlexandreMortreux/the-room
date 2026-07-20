#!/usr/bin/env python3
"""THE ROOM — an autonomous daily BTC digest.

Two AI analysts with opposing methodologies (Oracle and Guardian) break down
one market signal of the day, lock in mutually exclusive, verifiable predictions
and keep a public accuracy score in ledger.csv.

Runs daily from GitHub Actions, no servers. Run modes are selected by env vars
(see the constants below): DRY_RUN, STAGE, FORCE, or production.
"""

import csv
import json
import os
import re
import shutil
import sys
import tempfile
import random
import time
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import anthropic
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
# Run modes (precedence: DRY_RUN > FORCE > STAGE > production):
#   DRY_RUN=1  local debug — print instead of sending, ledger_dry.csv
#   STAGE=1    full run to the STAGING channel, ledger_staging.csv, [STAGE] logs
#   FORCE=1    production run that bypasses the daily dedup (manual go)
#   (none)     production (scheduled cron)
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE = os.environ.get("FORCE") == "1"
STAGE = os.environ.get("STAGE") == "1" and not FORCE and not DRY_RUN
if DRY_RUN:
    LEDGER_FILE = os.path.join(ROOT, "ledger_dry.csv")
elif STAGE:
    LEDGER_FILE = os.path.join(ROOT, "ledger_staging.csv")
else:
    LEDGER_FILE = os.path.join(ROOT, "ledger.csv")
PROMPTS_DIR = os.path.join(ROOT, "prompts")

MODEL_CLASSIFIER = "claude-haiku-4-5-20251001"
MODEL_DEBATE = "claude-sonnet-4-6"

HTTP_TIMEOUT = 15
PRICE_SOURCE = "binance_btcusdt_1d_close"
NEWS_URL = "https://t.me/s/markettwits"
REPO_URL = os.environ.get("REPO_URL", "https://github.com/AlexandreMortreux/the-room")
# direct link to the ledger file (overridable via LEDGER_URL)
LEDGER_URL = os.environ.get("LEDGER_URL", f"{REPO_URL}/blob/main/ledger.csv")
DAY0 = date(2026, 7, 6)  # first prediction pair; that day is Day 1
DISCLAIMER = (
    "<i>Not financial advice. Predictions are an experiment — "
    f'<a href="{LEDGER_URL}">open ledger</a>.</i>'
)

# X (Twitter) broadcast — production only, and only when all 4 OAuth-1.0a keys
# are present (a new account is pay-per-use: ~$0.015/post, $0.20 with a link).
# In DRY_RUN/STAGE, x_post() logs the composed text instead of sending.
X_KEYS = {k: os.environ.get(k, "") for k in
          ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}
X_ENABLED = not DRY_RUN and not STAGE and all(X_KEYS.values())

LEDGER_FIELDS = [
    "id", "created_utc", "agent", "asset", "direction", "level",
    "horizon_h", "confidence", "price_source", "price_at_call",
    "expires_utc", "resolved_utc", "price_at_expiry", "result", "brier",
]


def log(msg):
    print(f"[the-room]{' [STAGE]' if STAGE else ''} {msg}", flush=True)


def tg_channel():
    """Target chat id: the staging channel in STAGE mode, else production."""
    if STAGE:
        return os.environ.get("TG_CHANNEL_ID_STAGING", "")
    return os.environ.get("TG_CHANNEL_ID", "")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# HTTP: 15s timeout, one retry; None when a source is unavailable
# ---------------------------------------------------------------------------

def http_get(url, params=None, headers=None):
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            if attempt == 0:
                time.sleep(3)
    log(f"source unavailable: {url} ({last_err})")
    return None


def http_get_json(url, params=None):
    resp = http_get(url, params=params)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError as e:
        log(f"bad json from {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Data payload: Binance spot/futures + Fear & Greed, all keyless
# ---------------------------------------------------------------------------

# data-api.binance.vision is Binance's official market-data mirror without
# geoblocking: api.binance.com returns 451 from GitHub Actions IPs (US)
SPOT_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")


def fetch_klines():
    """Daily BTCUSDT candles: [open_time, o, h, l, close, vol, close_time, ...]."""
    for host in SPOT_HOSTS:
        data = http_get_json(
            f"{host}/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 8},
        )
        if data:
            return data
    return None


def fetch_current_price(klines):
    if klines:
        return float(klines[-1][4])
    for host in SPOT_HOSTS:
        data = http_get_json(f"{host}/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
        if data:
            return float(data["price"])
    raise RuntimeError("cannot determine current BTC price: Binance spot unavailable")


def build_data_payload(klines, current_price):
    payload = {"current_price": current_price}

    if klines:
        finished = klines[:-1]
        payload["daily_closes_7d"] = [
            {
                "date": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                "close": float(k[4]),
            }
            for k in finished
        ]
    else:
        payload["daily_closes_7d"] = "unavailable"

    # futures data: Binance fapi geo-blocks GitHub Actions IPs, so the
    # fallback is OKX public endpoints (BTC-USDT-SWAP)
    funding = http_get_json(
        "https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"}
    )
    if funding:
        payload["funding_rate"] = float(funding["lastFundingRate"])
    else:
        okx = http_get_json(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": "BTC-USDT-SWAP"},
        )
        if okx and okx.get("data"):
            payload["funding_rate"] = float(okx["data"][0]["fundingRate"])
            payload["funding_rate_source"] = "okx_btc_usdt_swap"
        else:
            payload["funding_rate"] = "unavailable"

    oi = http_get_json(
        "https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": "BTCUSDT"}
    )
    if oi:
        payload["open_interest_btc"] = float(oi["openInterest"])
    else:
        okx = http_get_json(
            "https://www.okx.com/api/v5/public/open-interest",
            params={"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
        )
        if okx and okx.get("data"):
            payload["open_interest_btc"] = float(okx["data"][0]["oiCcy"])
            payload["open_interest_source"] = "okx_btc_usdt_swap"
        else:
            payload["open_interest_btc"] = "unavailable"

    # 30 days so the extreme-fear streak is counted correctly even past a week;
    # the model is shown only the 7-day window but cites the streak as a number.
    fng = http_get_json("https://api.alternative.me/fng/", params={"limit": 30})
    if fng and fng.get("data"):
        hist = fng["data"]  # most-recent first
        payload["fear_greed_7d"] = [
            {
                "date": datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc).strftime("%Y-%m-%d"),
                "value": int(d["value"]),
                "classification": d["value_classification"],
            }
            for d in hist[:7]
        ]
        payload["fear_greed_now"] = {"value": int(hist[0]["value"]),
                                     "classification": hist[0]["value_classification"]}
        # consecutive most-recent days classified "Extreme Fear" — computed here,
        # NOT counted by the model (which drifts, e.g. "seven" one day, "five" next)
        streak = 0
        for d in hist:
            if d["value_classification"] == "Extreme Fear":
                streak += 1
            else:
                break
        payload["extreme_fear_streak_days"] = streak
    else:
        payload["fear_greed_7d"] = "unavailable"
        payload["extreme_fear_streak_days"] = "unavailable"

    # Derived numbers are computed here, not by the model: 7-day low/high,
    # % changes, funding % — ready-made, so the model only cites them.
    payload["current_price_display"] = f"${current_price:,.0f}"
    if isinstance(payload["daily_closes_7d"], list) and payload["daily_closes_7d"]:
        closes = [c["close"] for c in payload["daily_closes_7d"]]
        payload["low_7d"] = min(closes)
        payload["high_7d"] = max(closes)
        payload["prev_daily_close"] = closes[-1]
        payload["weekly_change_pct"] = round((current_price / closes[0] - 1) * 100, 2)
        payload["day_change_pct"] = round((current_price / closes[-1] - 1) * 100, 2)
    if isinstance(payload.get("funding_rate"), float):
        payload["funding_rate_pct"] = round(payload["funding_rate"] * 100, 4)

    return payload


# ---------------------------------------------------------------------------
# News: t.me/s/markettwits over the last 24 hours
# ---------------------------------------------------------------------------

def fetch_news(now):
    resp = http_get(
        NEWS_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        },
    )
    if resp is None:
        return []
    news = []
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for block in soup.select(".tgme_widget_message"):
            text_el = block.select_one(".tgme_widget_message_text")
            time_el = block.select_one("time[datetime]")
            if not text_el or not time_el:
                continue
            posted = parse_iso(time_el["datetime"])
            if now - posted > timedelta(hours=24):
                continue
            text = text_el.get_text(" ", strip=True)
            if not text:
                continue
            news.append({"datetime": iso(posted), "text": text[:600]})
    except Exception as e:
        log(f"news parse failed, continuing with empty list: {e}")
        return []
    return news[-50:]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def load_ledger():
    # In non-production modes, seed the throwaway ledger from the real one so
    # resolution and track records are realistic; production edits ledger.csv.
    prod = os.path.join(ROOT, "ledger.csv")
    if LEDGER_FILE != prod and not os.path.exists(LEDGER_FILE) and os.path.exists(prod):
        shutil.copyfile(prod, LEDGER_FILE)
    if not os.path.exists(LEDGER_FILE):
        return []
    with open(LEDGER_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_ledger(rows):
    with open(LEDGER_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def season_score(rows):
    score = {
        "oracle": {"wins": 0, "losses": 0, "brier_sum": 0.0, "resolved": 0},
        "guardian": {"wins": 0, "losses": 0, "brier_sum": 0.0, "resolved": 0},
    }
    for r in rows:
        # daily game only — weekly (168h) bets are scored in the ledger separately
        if r["agent"] not in score or r["result"] not in ("win", "loss"):
            continue
        if r.get("horizon_h") != "24":
            continue
        s = score[r["agent"]]
        s["wins" if r["result"] == "win" else "losses"] += 1
        s["resolved"] += 1
        if r["brier"]:
            s["brier_sum"] += float(r["brier"])
    return score


def day_number(now):
    """Ordinal experiment day (first production post = Day 1)."""
    return (now.date() - DAY0).days + 1


def case_number(rows, created_utc=None):
    """1-based index of a daily prediction pair; a new pair gets the next number."""
    dates = sorted({r["created_utc"] for r in rows if r.get("horizon_h") == "24"})
    if created_utc in dates:
        return dates.index(created_utc) + 1
    return len(dates) + 1


def last_daily_winner(rows):
    """Agent who won the most recently resolved daily pair, or None."""
    res = [r for r in rows if r["result"] in ("win", "loss")
           and r.get("horizon_h") == "24" and r["resolved_utc"]]
    if not res:
        return None
    latest = max(r["resolved_utc"] for r in res)
    return next((r["agent"] for r in res if r["resolved_utc"] == latest and r["result"] == "win"), None)


def build_season_line(rows, now, emoji=True):
    """Unified score line, one source everywhere:
    'Season · Day N: 🔮 Oracle X : Y Guardian 🛡' (X:Y = each agent's wins).
    Cards pass emoji=False (matplotlib can't render the agent emojis)."""
    sc = season_score(rows)
    o, g = sc["oracle"]["wins"], sc["guardian"]["wins"]
    head = f"Season · Day {day_number(now)}: "
    return head + (f"🔮 Oracle {o} : {g} Guardian 🛡" if emoji
                   else f"Oracle {o} : {g} Guardian")


def build_case_line(case_no, level, resolve_dt):
    """The deterministic tail of the setup message [1] — Case, watershed line and
    the exact resolve date (D+1 daily close). One source of truth for the setup
    and the debate validator, so the length check matches what actually posts."""
    return f"Case {case_no} · line ${level:,.0f} · resolves {resolve_dt:%b %d} close"


def build_setup_message(setup_text, case_no, level, resolve_dt):
    return f"{setup_text.strip()}\n{build_case_line(case_no, level, resolve_dt)}"


def build_tweet_draft(rows, predictions, now):
    """Ready-to-post tweet draft: Day N + score line + today's bet + repo link."""
    level = float({p["agent"]: p for p in predictions}["oracle"]["level"])
    return "\n".join([
        f"THE ROOM — Day {day_number(now)}",
        build_season_line(rows, now, emoji=False),
        f"Today: Oracle above ${level:,.0f} vs Guardian below ${level:,.0f}",
        REPO_URL,
    ])


# ---------------------------------------------------------------------------
# Step 1: resolve expired predictions against the Binance daily close
# ---------------------------------------------------------------------------

def resolve_close_date(created_utc, horizon_h):
    """Date of the Binance daily candle a pair settles against: a pair made on UTC
    day D with a 24h horizon settles on day D+1's daily close (a 168h weekly on
    D+7). That candle is labelled by its open date and prints at 00:00 UTC the next
    day. Single source of truth for the resolver and the bet card."""
    return parse_iso(created_utc).date() + timedelta(days=int(horizon_h) // 24)


def resolve_pending(rows, klines, now):
    """Resolve each pending pair against its OWN daily close — the candle whose
    date == the pair's resolve date (D+1 daily, D+7 weekly), never merely 'the
    first close after expiry'. Invariants:
      * a pair resolves only against that exact-dated candle;
      * one daily close settles exactly one pair — if two same-horizon pairs map
        to the same close, resolve NEITHER and alert, so a single close can never
        double-count two cases (the Case 9/10 incident)."""
    if not klines:
        log("klines unavailable, resolution skipped")
        return []
    # finished daily candles indexed by their UTC open-date (the candle's day label)
    by_date = {}
    for k in klines:
        if k[6] / 1000 <= now.timestamp():
            by_date[datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).date()] = k
    # pending rows grouped into pairs by created_utc, each with its target close
    pairs = {}
    for row in rows:
        if row["result"] == "pending":
            pairs.setdefault(row["created_utc"], []).append(row)
    target = {cu: resolve_close_date(cu, pair[0]["horizon_h"]) for cu, pair in pairs.items()}
    claims = Counter((target[cu], pair[0]["horizon_h"]) for cu, pair in pairs.items())
    resolved = []
    for cu, pair in pairs.items():
        rd = target[cu]
        if claims[(rd, pair[0]["horizon_h"])] > 1:
            log(f"resolution invariant: >1 pending pair maps to the {rd} close — "
                f"refusing to resolve {cu}")
            alert_owner(f"⚠️ THE ROOM: two pending pairs map to the {rd} daily "
                        f"close — resolution halted, nothing closed")
            continue
        candle = by_date.get(rd)
        if candle is None:
            continue  # that day's daily close hasn't printed yet — stays pending
        close = float(candle[4])
        for row in pair:
            level = float(row["level"])
            win = close > level if row["direction"] == "above" else close < level
            confidence = float(row["confidence"])
            outcome = 1.0 if win else 0.0
            row["result"] = "win" if win else "loss"
            row["price_at_expiry"] = f"{close:.2f}"
            row["resolved_utc"] = iso(now)
            row["brier"] = f"{(confidence - outcome) ** 2:.4f}"
            resolved.append(row)
    return resolved


def build_resolution_post(resolved, rows, now):
    close = float(resolved[0]["price_at_expiry"])
    winners = [r["agent"] for r in resolved if r["result"] == "win"]
    if winners:
        names = {"oracle": "🔮 Oracle", "guardian": "🛡 Guardian"}
        point_line = "Point goes to " + " and ".join(names[w] for w in winners) + "."
    else:
        point_line = "Nobody scores — both missed."

    case_no = case_number(rows, resolved[0]["created_utc"])
    lines = [f"<b>Case №{case_no} closed.</b>",
             f"🏁 <b>Resolution</b>: BTC daily close — <b>${close:,.0f}</b>", ""]
    for r in resolved:
        emoji = "🔮" if r["agent"] == "oracle" else "🛡"
        arrow = "above" if r["direction"] == "above" else "below"
        mark = "✅" if r["result"] == "win" else "❌"
        lines.append(
            f"{emoji} {r['agent'].capitalize()}: {arrow} ${float(r['level']):,.0f} "
            f"({int(round(float(r['confidence']) * 100))}%) — {mark} {r['result']}"
        )
    verdict = []
    losers = [r for r in resolved if r["result"] == "loss"]
    if losers:
        lo = losers[0]
        name = "🔮 Oracle" if lo["agent"] == "oracle" else "🛡 Guardian"
        plain = "close higher" if lo["direction"] == "above" else "close lower"
        verdict = [f"<i>{name} expected BTC to {plain} — the market did the opposite.</i>", ""]
    lines += ["", point_line, "", *verdict, build_season_line(rows, now)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Track records over the last 14 days
# ---------------------------------------------------------------------------

def build_track_records(rows, now):
    records = {}
    cutoff = now - timedelta(days=14)
    for agent in ("oracle", "guardian"):
        recent = [
            r for r in rows
            if r["agent"] == agent
            and r["result"] in ("win", "loss")
            and parse_iso(r["created_utc"]) >= cutoff
        ]
        wins = sum(1 for r in recent if r["result"] == "win")
        losses = len(recent) - wins
        briers = [float(r["brier"]) for r in recent if r["brier"]]
        misses = sorted(
            (r for r in recent if r["result"] == "loss"),
            key=lambda r: r["created_utc"],
        )
        last_miss = None
        if misses:
            m = misses[-1]
            arrow = "above" if m["direction"] == "above" else "below"
            last_miss = {
                "prediction": (
                    f"BTC {arrow} {float(m['level']):.0f} within {m['horizon_h']}h, "
                    f"confidence {m['confidence']}"
                ),
                "fact": f"daily close {m['price_at_expiry']}",
                "date": m["created_utc"][:10],
            }
        records[agent] = {
            "wins": wins,
            "losses": losses,
            "avg_brier": round(sum(briers) / len(briers), 4) if briers else None,
            "last_miss": last_miss,
        }
    return records


def build_past_calls(rows, now):
    """Verbatim-quotable past-call strings from the ledger (last 14 days).
    Agents must copy these exactly — never recall a level from memory."""
    cutoff = now - timedelta(days=14)
    recent = sorted(
        (r for r in rows if r["result"] in ("win", "loss")
         and parse_iso(r["created_utc"]) >= cutoff),
        key=lambda r: r["created_utc"],
    )
    return [
        f"{r['created_utc'][:10]} · {r['agent'].capitalize()} · "
        f"{r['direction']} ${float(r['level']):,.0f} → close "
        f"${float(r['price_at_expiry']):,.0f} · {r['result']}"
        for r in recent
    ]


def build_allowed_dollars(rows, data_payload, current_price):
    """Every $-amount the debate may legitimately contain: live price + data
    numbers + all ledger levels/closes. Guards against invented/misquoted levels."""
    vals = {float(current_price)}
    for k in ("low_7d", "high_7d", "prev_daily_close"):
        v = data_payload.get(k)
        if isinstance(v, (int, float)):
            vals.add(float(v))
    dc = data_payload.get("daily_closes_7d")
    if isinstance(dc, list):
        vals.update(float(c["close"]) for c in dc)
    for r in rows:
        if r["result"] in ("win", "loss"):
            vals.add(float(r["level"]))
            if r["price_at_expiry"]:
                vals.add(float(r["price_at_expiry"]))
    return vals


# ---------------------------------------------------------------------------
# Claude: strict-JSON calls, one retry with the error text
# ---------------------------------------------------------------------------

def read_prompt(name):
    with open(os.path.join(PROMPTS_DIR, name), encoding="utf-8") as f:
        return f.read()


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model response")
    return json.loads(text[start:end + 1])


def call_claude_json(client, model, system, user, max_tokens, validate, max_attempts=2):
    messages = [{"role": "user", "content": user}]
    last_err = None
    for attempt in range(max_attempts):
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages
        )
        text = resp.content[0].text
        try:
            data = extract_json(text)
            validate(data)
            return data
        except (ValueError, KeyError, TypeError) as e:
            last_err = e
            log(f"model JSON invalid (attempt {attempt + 1}/{max_attempts}): {e}")
            messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        f"Your response is invalid: {e}. "
                        "Return corrected strict JSON, no preamble or markdown."
                    ),
                },
            ]
    raise RuntimeError(f"model returned invalid JSON after {max_attempts} attempts: {last_err}")


def validate_classifier(data):
    ds = data.get("day_signal")
    if ds is None:
        return
    if not isinstance(ds, dict):
        raise ValueError("'day_signal' must be an object or null")
    headlines = ds.get("headlines")
    if not isinstance(headlines, list) or not 2 <= len(headlines) <= 4:
        raise ValueError("'headlines' must be a list of 2-4 items")
    if not all(isinstance(h, str) and h.strip() for h in headlines):
        raise ValueError("each headline must be a non-empty string")
    if not isinstance(ds.get("synthesis"), str) or not ds["synthesis"].strip():
        raise ValueError("'synthesis' must be a non-empty string")


def classify_news(client, news):
    """Returns a synthesis of 2-4 related BTC news items (day_signal) or None."""
    if not news:
        return None
    indexed = [
        {"id": i, "datetime": n["datetime"], "text": n["text"]}
        for i, n in enumerate(news)
    ]
    user = "News from the last 24 hours:\n" + json.dumps(indexed, ensure_ascii=False, indent=1)
    data = call_claude_json(
        client, MODEL_CLASSIFIER, read_prompt("classifier.txt"),
        user, max_tokens=1200, validate=validate_classifier,
    )
    return data.get("day_signal")


DOLLAR_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def _claim_int(tok):
    tok = tok.lower()
    return int(tok) if tok.isdigit() else _NUM_WORDS.get(tok)


def check_quant_claims(post_html, payload):
    """Best-effort consistency guard: return warnings (never raises) where a
    counted or rounded claim in the post diverges from the deterministic feed
    number — the day-to-day drift like 'seven straight days' vs 'five'. Counts
    are compared exactly; funding/weekly/OI to their payload value."""
    text = re.sub(r"<[^>]+>", " ", post_html or "")
    warnings = []

    streak = payload.get("extreme_fear_streak_days")
    if isinstance(streak, int):
        # only compare a count that is explicitly tied to EXTREME fear (the metric
        # we track) — a claim about the plain "fear" zone is not this streak
        for m in re.finditer(
                r"extreme fear[^.]{0,25}?\b(\w+)\s+(?:days?|sessions?)"
                r"|\b(\w+)\s+(?:straight |consecutive )?(?:days?|sessions?)[^.]{0,25}?extreme fear",
                text, re.I):
            n = _claim_int(m.group(1) or m.group(2))
            if n is not None and n != streak:
                warnings.append(f"'{m.group(0).strip()}' vs extreme_fear_streak_days={streak}")

    def _pct(field, keyword, tol):
        v = payload.get(field)
        if not isinstance(v, (int, float)):
            return
        near = rf"{keyword}[^.]{{0,40}}?(-?\d+\.?\d*)\s*%|(-?\d+\.?\d*)\s*%[^.]{{0,20}}?{keyword}"
        for m in re.finditer(near, text, re.I):
            raw = m.group(1) or m.group(2)
            if abs(float(raw) - v) > tol:
                warnings.append(f"{field} claim {raw}% vs {v}")

    _pct("funding_rate_pct", "funding", 0.0001)
    _pct("weekly_change_pct", "week", 0.01)

    oi = payload.get("open_interest_btc")
    if isinstance(oi, (int, float)):
        for m in re.finditer(r"(?:open interest|\bOI\b)[^.]{0,40}?([\d,]{3,})|([\d,]{3,})\s*BTC", text, re.I):
            raw = (m.group(1) or m.group(2)).replace(",", "")
            if raw.isdigit() and abs(int(raw) - round(oi)) > max(1, 0.005 * oi):
                warnings.append(f"open_interest claim {raw} vs ~{round(oi)}")
    return warnings


def make_debate_validator(current_price, allowed_dollars, case_no, resolve_dt):
    """Validates the live-debate JSON: the six message fields, their hard length
    limits (exceeding = fail, so retries enforce it), no figures/promise words in
    the argument messages, and two predictions at one watershed level."""
    text_fields = ("setup", "oracle_open", "guardian_attack", "oracle_jab", "card_caption")
    reply_fields = ("oracle_open", "guardian_attack", "oracle_jab")
    limits = {"oracle_open": 280, "guardian_attack": 280, "oracle_jab": 160, "card_caption": 120}

    def validate(data):
        for f in text_fields:
            if not isinstance(data.get(f), str) or not data[f].strip():
                raise ValueError(f"'{f}' must be a non-empty string")

        preds = data.get("predictions")
        if not isinstance(preds, list) or len(preds) != 2:
            raise ValueError("'predictions' must contain exactly 2 items")
        if {p.get("agent") for p in preds} != {"oracle", "guardian"}:
            raise ValueError("agents must be exactly 'oracle' and 'guardian'")
        if {p.get("direction") for p in preds} != {"above", "below"}:
            raise ValueError("directions must be mutually exclusive: one 'above', one 'below'")
        if len({round(float(p["level"]), 2) for p in preds}) != 1:
            raise ValueError("both predictions must reference the same level")
        for p in preds:
            if p.get("asset") != "BTC":
                raise ValueError("asset must be 'BTC'")
            if int(p["horizon_h"]) != 24:
                raise ValueError("horizon_h must be 24")
            conf = float(p["confidence"])
            if not 0.55 <= conf <= 0.80:
                raise ValueError(f"confidence {conf} outside [0.55, 0.80]")
            lvl = float(p["level"])
            if abs(lvl - current_price) > 0.15 * current_price:
                raise ValueError(f"level {lvl} outside ±15% of current price {current_price}")
            if abs(lvl - current_price) < 0.001 * current_price:
                raise ValueError(
                    f"level {lvl} must be a structural watershed distinct from the "
                    f"current price {current_price:.0f}, not the current price")

        level = float(preds[0]["level"])
        # [1] length includes the code-appended Case line, so check the real message
        setup_msg = build_setup_message(data["setup"], case_no, level, resolve_dt)
        if len(setup_msg) >= 220:
            raise ValueError(f"setup message is {len(setup_msg)} chars (limit 220) — shorten the setup")
        for f, lim in limits.items():
            n = len(data[f].strip())
            if n >= lim:
                raise ValueError(f"'{f}' is {n} chars (limit {lim}) — shorten it")

        # the argument messages carry NO figures and NO promise words — every
        # number lives on the card, the words do the fighting
        for f in reply_fields:
            t = data[f]
            if "$" in t or re.search(r"\d\s*%", t):
                raise ValueError(f"'{f}' must carry no $ amount or % — numbers live on the card")
            if re.search(r"\b(guaranteed|will)\b", t, re.I):
                raise ValueError(f"'{f}' uses a forbidden promise word (guaranteed/will)")
        if "$" in data["card_caption"] or "%" in data["card_caption"]:
            raise ValueError("'card_caption' must carry no numbers")

        # dollar integrity: the setup is the only argument text that may name a $
        # figure, and it must be a known number (payload, ledger level, watershed)
        allowed = set(allowed_dollars) | {level}
        lo, hi = 0.5 * current_price, 2.0 * current_price
        for raw in DOLLAR_RE.findall(data["setup"]):
            v = float(raw.replace(",", ""))
            if lo <= v <= hi and not any(abs(v - a) <= max(2.0, 0.0005 * a) for a in allowed):
                raise ValueError(
                    f"dollar value ${v:,.0f} in the setup matches no ledger level/close "
                    f"or data_payload number")
    return validate


def make_weekly_validator(current_price):
    def validate(data):
        preds = data["predictions"]
        if not isinstance(preds, list) or len(preds) != 2:
            raise ValueError("'predictions' must contain exactly 2 items")
        if {p.get("agent") for p in preds} != {"oracle", "guardian"}:
            raise ValueError("agents must be oracle and guardian")
        if {p.get("direction") for p in preds} != {"above", "below"}:
            raise ValueError("directions must be one above, one below")
        if len({round(float(p["level"]), 2) for p in preds}) != 1:
            raise ValueError("both predictions must share one level")
        for p in preds:
            if int(p["horizon_h"]) != 168:
                raise ValueError("horizon_h must be 168")
            c = float(p["confidence"])
            if not 0.55 <= c <= 0.80:
                raise ValueError(f"confidence {c} outside [0.55, 0.80]")
            lvl = float(p["level"])
            if abs(lvl - current_price) > 0.15 * current_price:
                raise ValueError(f"level {lvl} outside ±15% of current price")
            if abs(lvl - current_price) < 0.001 * current_price:
                raise ValueError("level must be a watershed distinct from the current price")
    return validate


def generate_weekly(client, data_payload, current_price):
    """Two mutually exclusive weekly (168h) predictions at one watershed level."""
    user = read_prompt("weekly.txt") + "\n\nInput data:\n" + json.dumps(
        {"data_payload": data_payload, "current_price": current_price},
        ensure_ascii=False, indent=1)
    data = call_claude_json(
        client, MODEL_DEBATE, "You output strict JSON only, no preamble.", user,
        max_tokens=400, validate=make_weekly_validator(current_price), max_attempts=3)
    return data["predictions"]


def run_debate(client, signal, data_payload, track_records, current_price,
               past_calls, allowed_dollars, case_no, resolve_dt, last_winner):
    system = read_prompt("oracle.txt") + "\n\n" + read_prompt("guardian.txt")
    # The orchestrator (full structure + JSON schema) is ALWAYS included; on a
    # quiet day the fallback guidance is appended, so fallback posts get the same
    # schema instead of the model guessing the format.
    instructions = read_prompt("orchestrator.txt")
    if not signal:
        instructions += "\n\n--- QUIET MARKET (no signal today) ---\n" + read_prompt("fallback.txt")
    inputs = {
        "day_signal": signal,
        "data_payload": data_payload,
        "track_records": track_records,
        "past_calls": past_calls,
        # who won the last resolved daily pair, so Oracle can open [2] with a
        # one-phrase admission when it lost
        "last_result": {"winner": last_winner},
    }
    user = instructions + "\n\nInput data:\n" + json.dumps(
        inputs, ensure_ascii=False, indent=1
    )
    return call_claude_json(
        client, MODEL_DEBATE, system, user, max_tokens=2500,
        validate=make_debate_validator(current_price, allowed_dollars, case_no, resolve_dt),
        max_attempts=4,
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

_DRY_MID = [1000]  # fake, monotonically increasing message ids for DRY_RUN


def tg_call(method, payload):
    """Returns the sent message's id (int), or None when Telegram omits one."""
    if DRY_RUN:
        print(f"\n[DRY_RUN] Telegram {method}:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        _DRY_MID[0] += 1
        return _DRY_MID[0]
    token = os.environ["TG_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            body = resp.json()
            if body.get("ok"):
                return (body.get("result") or {}).get("message_id")
            last_err = body.get("description", resp.text)
        except (requests.RequestException, ValueError) as e:
            last_err = e
        if attempt == 0:
            time.sleep(3)
    raise RuntimeError(f"Telegram {method} failed: {last_err}")


def tg_send_photo(path, caption=None, reply_to=None):
    """Sends the PNG under its meaningful basename; optional HTML caption.
    Returns the sent message id."""
    filename = os.path.basename(path)
    if DRY_RUN:
        print(f"\n[DRY_RUN] Telegram sendPhoto: {filename}")
        if caption:
            print(f"[DRY_RUN] caption:\n{caption}")
        _DRY_MID[0] += 1
        return _DRY_MID[0]
    token = os.environ["TG_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": tg_channel()}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    if reply_to is not None:
        data["reply_to_message_id"] = reply_to
    last_err = None
    for attempt in range(2):
        try:
            with open(path, "rb") as f:
                resp = requests.post(url, data=data,
                                     files={"photo": (filename, f)}, timeout=30)
            body = resp.json()
            if body.get("ok"):
                return (body.get("result") or {}).get("message_id")
            last_err = body.get("description", resp.text)
        except (requests.RequestException, ValueError, OSError) as e:
            last_err = e
        if attempt == 0:
            time.sleep(3)
    raise RuntimeError(f"Telegram sendPhoto failed: {last_err}")


def tg_send_message(html, chat_id=None, reply_to=None):
    """Returns the sent message id (for building a reply chain)."""
    payload = {
        "chat_id": chat_id or tg_channel(),
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    return tg_call("sendMessage", payload)


def tg_send_document(path, chat_id=None):
    """Sends a PNG as an uncompressed file (for the draft tweet's card)."""
    filename = os.path.basename(path)
    if DRY_RUN:
        print(f"\n[DRY_RUN] Telegram sendDocument -> {chat_id or tg_channel()}: {filename}")
        return
    token = os.environ["TG_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    last_err = None
    for attempt in range(2):
        try:
            with open(path, "rb") as f:
                resp = requests.post(url, data={"chat_id": chat_id or tg_channel()},
                                     files={"document": (filename, f)}, timeout=30)
            body = resp.json()
            if body.get("ok"):
                return
            last_err = body.get("description", resp.text)
        except (requests.RequestException, ValueError, OSError) as e:
            last_err = e
        if attempt == 0:
            time.sleep(3)
    raise RuntimeError(f"Telegram sendDocument failed: {last_err}")


def tg_send_poll(predictions, reply_to=None):
    payload = {
        "chat_id": tg_channel(),
        "question": "Who's right today?",
        "options": ["🔮 UP", "🛡 DOWN"],
        "is_anonymous": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    return tg_call("sendPoll", payload)


def _thread_delay():
    """Human-like pause between chained messages — the thread should read like a
    live argument, not a volley. Skipped only in DRY_RUN (no real posting)."""
    if not DRY_RUN:
        time.sleep(random.uniform(30, 90))


def render_bet_card(rows, predictions, data_payload, current_price, now):
    """Renders the v2 bet card PNG and returns its path."""
    import svg_card
    preds = {p["agent"]: p for p in predictions}
    o, g = preds["oracle"], preds["guardian"]
    level = float(o["level"])
    case_no = case_number(rows)
    sc = season_score(rows)
    dc = data_payload.get("day_change_pct")
    resolve_dt = resolve_close_date(iso(now), 24)  # the D+1 close it settles on
    path = os.path.join(tempfile.gettempdir(),
                        f"theroom_{now:%Y-%m-%d}_case{case_no}_bet.png")
    svg_card.build_card_v2(
        path, case_no=case_no, date_label=f"{now:%b} {now.day}",
        day_n=day_number(now), oracle_wins=sc["oracle"]["wins"],
        guardian_wins=sc["guardian"]["wins"], last_winner=last_daily_winner(rows),
        level=level, btc_price=current_price,
        btc_change=float(dc) if isinstance(dc, (int, float)) else 0.0,
        resolve_label=f"Resolves at {resolve_dt:%b} {resolve_dt.day} close",
        bull_conf=float(o["confidence"]), bull_reason=str(o.get("driver", "")).strip(),
        bear_conf=float(g["confidence"]), bear_reason=str(g.get("driver", "")).strip())
    return path


# ---------------------------------------------------------------------------
# X (Twitter) broadcast — notarial channel voice, one line per post
# ---------------------------------------------------------------------------

def build_x_morning(case_no, predictions, rows, resolve_dt):
    level = float({p["agent"]: p for p in predictions}["oracle"]["level"])
    sc = season_score(rows)
    return (f"Case {case_no}. Oracle says up, Guardian says down. "
            f"Line ${level:,.0f}. Score {sc['oracle']['wins']}:{sc['guardian']['wins']}. "
            f"Resolves {resolve_dt:%b %d} close.")


def build_x_evening(case_no, winner, rows, ledger_url):
    sc = season_score(rows)
    point = {"oracle": "Point Oracle.", "guardian": "Point Guardian."}.get(winner, "No point.")
    return (f"Case {case_no} closed. {point} "
            f"{sc['oracle']['wins']}:{sc['guardian']['wins']}. Ledger: {ledger_url}")


def x_post(text, image_path=None):
    """Broadcast to X (production only). In DRY_RUN/STAGE or without keys, log the
    composed text and return None. Raises on a real API error so the caller's
    isolated step alerts — X never blocks the main pipeline."""
    if not X_ENABLED:
        log(f"[X preview]{' (+image)' if image_path else ''} {text}")
        return None
    import tweepy
    api = tweepy.API(tweepy.OAuth1UserHandler(
        X_KEYS["X_API_KEY"], X_KEYS["X_API_SECRET"],
        X_KEYS["X_ACCESS_TOKEN"], X_KEYS["X_ACCESS_SECRET"]))
    media_ids = None
    if image_path and os.path.exists(image_path):
        media_ids = [api.media_upload(image_path).media_id]
    client = tweepy.Client(
        consumer_key=X_KEYS["X_API_KEY"], consumer_secret=X_KEYS["X_API_SECRET"],
        access_token=X_KEYS["X_ACCESS_TOKEN"], access_token_secret=X_KEYS["X_ACCESS_SECRET"])
    resp = client.create_tweet(text=text, media_ids=media_ids)
    tid = (resp.data or {}).get("id")
    log(f"posted to X: {tid}")
    return tid


# ---------------------------------------------------------------------------
# Sunday scorecard (by UTC+8)
# ---------------------------------------------------------------------------

def is_sunday_utc8(now):
    return (now + timedelta(hours=8)).weekday() == 6


def build_scorecard_stats(rows, now):
    resolved = [r for r in rows if r["result"] in ("win", "loss") and r.get("horizon_h") == "24"]
    if not resolved:
        return None
    week = [
        r for r in resolved
        if r["resolved_utc"] and parse_iso(r["resolved_utc"]) >= now - timedelta(days=7)
    ]

    def streak(agent):
        history = sorted(
            (r for r in resolved if r["agent"] == agent),
            key=lambda r: r["resolved_utc"],
        )
        if not history:
            return None
        last = history[-1]["result"]
        n = 0
        for r in reversed(history):
            if r["result"] != last:
                break
            n += 1
        return f"{'W' if last == 'win' else 'L'}{n}"

    def describe(r):
        return {
            "agent": r["agent"],
            "prediction": f"BTC {r['direction']} {float(r['level']):.0f}",
            "confidence": r["confidence"],
            "fact": r["price_at_expiry"],
            "result": r["result"],
            "brier": r["brier"],
        }

    score = season_score(rows)
    stats = {"season_line": build_season_line(rows, now), "season": {}, "week": {}}
    for agent in ("oracle", "guardian"):
        s = score[agent]
        stats["season"][agent] = {
            "accuracy_pct": round(100 * s["wins"] / s["resolved"], 1) if s["resolved"] else None,
            "mean_brier": round(s["brier_sum"] / s["resolved"], 3) if s["resolved"] else None,
            "streak": streak(agent),
        }
    week_scored = [r for r in week if r["brier"]]
    stats["week"]["best_call"] = (
        describe(min(week_scored, key=lambda r: float(r["brier"]))) if week_scored else None
    )
    stats["week"]["worst_call"] = (
        describe(max(week_scored, key=lambda r: float(r["brier"]))) if week_scored else None
    )
    return stats


def post_scorecard(client, rows, now):
    stats = build_scorecard_stats(rows, now)
    if stats is None:
        log("no resolved predictions yet, scorecard skipped")
        return
    system = (
        "You are the editor of THE ROOM, a daily BTC digest. From the data below, "
        "write the Sunday scorecard post in English: title '🏆 Weekly Scorecard', "
        "then the score line EXACTLY as given in season_line (do NOT use any W-L "
        "notation), accuracy %, mean Brier (use the mean_brier value as-is, format "
        "0.XXX), current streak, and the week's best and worst call with facts. "
        "Include one short line explaining Brier: "
        "'mean Brier, lower = better calibrated'. Dry, lightly ironic, "
        "≤20 seconds to read. Telegram HTML (<b>, <i>), no markdown. English only, no "
        "language mixing. Output the post text only, no preamble."
    )
    resp = client.messages.create(
        model=MODEL_DEBATE,
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": json.dumps(stats, ensure_ascii=False, indent=1)}],
    )
    tg_send_message(resp.content[0].text.strip() + "\n\n" + DISCLAIMER)
    log("scorecard posted")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def current_streak(rows, agent):
    """(result, length) of the agent's current streak by latest resolutions, or None."""
    history = sorted(
        (r for r in rows if r["agent"] == agent and r["result"] in ("win", "loss")
         and r.get("horizon_h") == "24"),
        key=lambda r: r["resolved_utc"] or r["created_utc"],
    )
    if not history:
        return None
    last = history[-1]["result"]
    n = 0
    for r in reversed(history):
        if r["result"] != last:
            break
        n += 1
    return last, n


def streak_leader(rows):
    """Agent with the longest current win streak, only when it's >= 2 — a streak
    of 1 just duplicates the card's 'Yesterday: point …', so it's suppressed."""
    best = None
    for agent in ("oracle", "guardian"):
        s = current_streak(rows, agent)
        if s and s[0] == "win" and s[1] >= 2 and (best is None or s[1] > best[1]):
            best = (agent, s[1])
    if not best:
        return None
    name = "🔮 Oracle" if best[0] == "oracle" else "🛡 Guardian"
    return f"{name} {best[1]}"


def append_predictions(rows, predictions, current_price, now, horizon_h=24, id_tag=""):
    expires = now + timedelta(hours=horizon_h)
    tag = f"{id_tag}-" if id_tag else ""
    for p in predictions:
        rows.append({
            "id": f"{now:%Y%m%d-%H%M}-{tag}{p['agent']}",
            "created_utc": iso(now),
            "agent": p["agent"],
            "asset": "BTC",
            "direction": p["direction"],
            "level": f"{float(p['level']):.2f}",
            "horizon_h": str(horizon_h),
            "confidence": f"{float(p['confidence']):.2f}",
            "price_source": PRICE_SOURCE,
            "price_at_call": f"{current_price:.2f}",
            "expires_utc": iso(expires),
            "resolved_utc": "",
            "price_at_expiry": "",
            "result": "pending",
            "brier": "",
        })


def alert_owner(text):
    """Best-effort failure alert to the owner's private chat (TG_OWNER_CHAT_ID)."""
    owner = os.environ.get("TG_OWNER_CHAT_ID", "")
    if not owner:
        return
    try:
        tg_send_message(text, chat_id=owner)
    except Exception as e:
        log(f"WARNING: owner alert failed: {e}")


def run_step(name, fn):
    """Run one pipeline step in isolation: on failure log the full traceback and
    alert the owner, but let the rest of the run continue. Returns True on success."""
    try:
        fn()
        return True
    except Exception as e:
        log(f"WARNING: step '{name}' failed: {e}\n{traceback.format_exc()}")
        alert_owner(f"⚠️ THE ROOM: step {name} failed — {e}")
        return False


def post_resolution(resolved, rows, now):
    """Template A resolution card with the full text as caption (one message);
    if the card fails, still post the text so the resolution is never lost. Then
    broadcasts the close to X (isolated — X never breaks the Telegram resolution)."""
    resolution_text = build_resolution_post(resolved, rows, now)
    wrow = next((r for r in resolved if r["result"] == "win"), None)
    winner = wrow["agent"] if wrow else None
    case_no = case_number(rows, resolved[0]["created_utc"])
    card_path = None
    try:
        import card as card_mod
        close_px = float(resolved[0]["price_at_expiry"])
        level = float(resolved[0]["level"])
        fname = (f"theroom_{now:%Y-%m-%d}_case{case_no}_closed_"
                 f"{winner.upper() if winner else 'SPLIT'}.png")
        leader = streak_leader(rows)
        path = os.path.join(tempfile.gettempdir(), fname)
        card_mod.build_resolution_card(
            path, close_px=close_px, level=level, winner=winner,
            preds={r["agent"]: float(r["confidence"]) for r in resolved},
            missed_by=abs(close_px - level), case_no=case_no,
            season_line=build_season_line(rows, now, emoji=False),
            streak=leader.replace("🔮 ", "").replace("🛡 ", "") if leader else None,
            date_label=f"{resolve_close_date(resolved[0]['created_utc'], resolved[0]['horizon_h']):%b %d}",
        )
        card_path = path
        caption = resolution_text if len(resolution_text) <= 1024 else None
        tg_send_photo(path, caption=caption)
        if caption is None:
            tg_send_message(resolution_text)
    except Exception as e:
        log(f"WARNING: resolution card failed, posting text only: {e}")
        tg_send_message(resolution_text)

    # X broadcast of the close — production only, isolated from the Telegram post
    try:
        x_post(build_x_evening(case_no, winner, rows, LEDGER_URL), card_path)
    except Exception as e:
        log(f"WARNING: X evening post failed: {e}")
        alert_owner(f"⚠️ THE ROOM: X evening post failed — {e}")


def main():
    now = utcnow()
    mode = "dry" if DRY_RUN else "stage" if STAGE else "prod"
    log(f"run started {iso(now)}, mode={mode}")
    client = anthropic.Anthropic()

    # 1. Resolve expired predictions
    rows = load_ledger()
    # STAGE-only test switch: back-date the oldest pending pair by 2 days so its
    # resolve date (D+1) lands on yesterday's already-printed close, forcing the
    # resolution path (Template A card + caption) on demand in staging.
    if STAGE and os.environ.get("STAGE_RESOLVE_TEST") == "1":
        days = sorted({r["created_utc"][:10] for r in rows if r["result"] == "pending"})
        if days:
            base = now.replace(hour=3, minute=0, second=0, microsecond=0) - timedelta(days=2)
            for r in rows:
                if r["result"] == "pending" and r["created_utc"][:10] == days[0]:
                    r["created_utc"] = iso(base)
                    r["expires_utc"] = iso(base + timedelta(hours=int(r["horizon_h"])))
            log(f"STAGE_RESOLVE_TEST: back-dated {days[0]} pending pair to force a resolution")
    klines = fetch_klines()
    current_price = fetch_current_price(klines)
    resolved = resolve_pending(rows, klines, now)
    if resolved:
        save_ledger(rows)  # persist all resolutions (daily + weekly) to the ledger
        log(f"resolved {len(resolved)} prediction(s)")
        # 2. Resolution card is the daily game only (weekly bets are scored in
        #    the ledger but not given their own resolution card).
        daily_resolved = [r for r in resolved if r["horizon_h"] == "24"]
        # group by pair (created_utc): if a backlog of >1 day resolves in one run
        # (e.g. a skipped run, or the ~24h resolution boundary), each case gets its
        # own clean resolution card instead of a card mixing two cases together
        by_pair = {}
        for r in daily_resolved:
            by_pair.setdefault(r["created_utc"], []).append(r)
        for created in sorted(by_pair):
            pair = by_pair[created]
            run_step("resolution", lambda pair=pair: post_resolution(pair, rows, now))

    # Idempotency: a scheduled production run does not post/write twice if a
    # prediction with today's UTC date already exists. DRY_RUN, STAGE and an
    # explicit FORCE=1 all bypass the dedup — previews and staging can rerun freely.
    today = iso(now)[:10]
    if not DRY_RUN and not STAGE and not FORCE and any(r["created_utc"][:10] == today for r in rows):
        log("already posted today, skipping debate/publish")
        return

    # 3-4. News and data payload
    news = fetch_news(now)
    log(f"news collected: {len(news)}")
    data_payload = build_data_payload(klines, current_price)

    # 5. Classifier (soft): a failure falls back to a quiet-market post, no crash
    try:
        signal = classify_news(client, news)
    except Exception as e:
        log(f"WARNING: classifier failed, using fallback: {e}")
        signal = None
    log(f"day signal: {len(signal['headlines'])} headlines" if signal else "day signal: none (fallback mode)")

    # 6. Track records + verbatim past-call strings + allowed dollar amounts
    track_records = build_track_records(rows, now)
    past_calls = build_past_calls(rows, now)
    allowed_dollars = build_allowed_dollars(rows, data_payload, current_price)

    # 7. Debate — prerequisite for the thread / card / poll / draft. On failure,
    #    alert and skip those dependents instead of crashing the run.
    case_no = case_number(rows)
    resolve_dt = resolve_close_date(iso(now), 24)
    try:
        debate = run_debate(client, signal, data_payload, track_records, current_price,
                            past_calls, allowed_dollars, case_no, resolve_dt,
                            last_daily_winner(rows))
    except Exception as e:
        log(f"WARNING: step 'debate' failed: {e}\n{traceback.format_exc()}")
        alert_owner(f"⚠️ THE ROOM: step debate failed — {e}")
        debate = None

    # Quantitative-consistency guard: warn (never fail) when a counted/rounded
    # claim in the debate diverges from the deterministic feed number. In staging
    # it also DMs the owner so drift is caught before it reaches the channel.
    if debate:
        debate_text = " ".join(debate[f] for f in
                               ("setup", "oracle_open", "guardian_attack", "oracle_jab", "card_caption"))
        quant_warnings = check_quant_claims(debate_text, data_payload)
        for w in quant_warnings:
            log(f"WARNING: quant-claim mismatch — {w}")
        if quant_warnings and STAGE:
            alert_owner("⚠️ THE ROOM quant-claim mismatches:\n" + "\n".join(quant_warnings))

    # 7b. Weekly watershed — on Sundays, one weekly (168h) call per agent, added
    #     to the ledger before the post so its footer line shows it. Once/Sunday.
    #     STAGE_WEEKLY_TEST=1 forces it in staging any day, for testing.
    weekly_due = is_sunday_utc8(now) or (STAGE and os.environ.get("STAGE_WEEKLY_TEST") == "1")
    if weekly_due and not any(
            r.get("horizon_h") == "168" and r["created_utc"][:10] == today for r in rows):
        def _weekly():
            wk = generate_weekly(client, data_payload, current_price)
            append_predictions(rows, wk, current_price, now, horizon_h=168, id_tag="weekly")
            save_ledger(rows)
            log("weekly bet appended to ledger")
        run_step("weekly_bet", _weekly)

    # 8. Publish — the day as a live reply-thread: [1] setup -> [2] Oracle ->
    #    [3] Guardian attack -> [4] Oracle jab, each a reply to the last with a
    #    30-90s human pause, then [5] the bet card and [6] the poll (standalone,
    #    not part of the reply chain). Sends are individually guarded so one
    #    failure doesn't drop the rest of the thread.
    if debate:
        card = {"path": None}

        def _daily_thread():
            level = float({p["agent"]: p for p in debate["predictions"]}["oracle"]["level"])
            chain = {"reply_to": None}

            def send(text, is_reply=True):
                try:
                    mid = tg_send_message(text, reply_to=chain["reply_to"] if is_reply else None)
                    if mid is not None:
                        chain["reply_to"] = mid
                except Exception as e:
                    log(f"WARNING: thread message failed: {e}")
                    alert_owner(f"⚠️ THE ROOM: a thread message failed — {e}")

            # [1] setup — root of the chain
            send(build_setup_message(debate["setup"], case_no, level, resolve_dt), is_reply=False)
            # [2]-[4] the argument, each a reply to the previous, with pauses
            for text in (debate["oracle_open"], debate["guardian_attack"], debate["oracle_jab"]):
                _thread_delay()
                send(text)
            # [5] bet card — standalone (all the numbers), 1-line caption + notice
            _thread_delay()
            try:
                card["path"] = render_bet_card(rows, debate["predictions"], data_payload, current_price, now)
                tg_send_photo(card["path"],
                              caption=f'{debate["card_caption"].strip()} · not financial advice')
            except Exception as e:
                log(f"WARNING: bet card failed: {e}\n{traceback.format_exc()}")
                alert_owner(f"⚠️ THE ROOM: bet card failed — {e}")
            # [6] poll — the finale
            _thread_delay()
            try:
                tg_send_poll(debate["predictions"])
            except Exception as e:
                log(f"WARNING: poll failed: {e}")
                alert_owner(f"⚠️ THE ROOM: poll failed — {e}")
        run_step("daily_thread", _daily_thread)

        # X broadcast of the morning bet — isolated (fail alerts, never blocks);
        # posts nothing outside production (logs a preview in staging/dry).
        run_step("x_morning", lambda: x_post(
            build_x_morning(case_no, debate["predictions"], rows, resolve_dt),
            card.get("path")))

        def _ledger_write():
            append_predictions(rows, debate["predictions"], current_price, now)
            save_ledger(rows)
            log("predictions appended to ledger")
        run_step("ledger_write", _ledger_write)

        # 9. Draft tweet to the owner — production only, skipped in STAGE.
        if not STAGE:
            def _draft_tweet():
                owner = os.environ.get("TG_OWNER_CHAT_ID", "")
                if not owner:
                    return
                tg_send_message(build_tweet_draft(rows, debate["predictions"], now), chat_id=owner)
                if card["path"] and os.path.exists(card["path"]):
                    tg_send_document(card["path"], chat_id=owner)
            run_step("draft_tweet", _draft_tweet)
    else:
        log("debate unavailable — skipping today card, signal, poll, draft tweet")

    # 10. Sunday scorecard (UTC+8) — independent isolated step.
    if is_sunday_utc8(now):
        run_step("scorecard", lambda: post_scorecard(client, rows, now))

    log("run finished")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        alert_owner(f"🛑 THE ROOM: run FAILED before/at a prerequisite — {e}")
        sys.exit(1)
