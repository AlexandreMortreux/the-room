#!/usr/bin/env python3
"""THE ROOM — автономный ежедневный BTC-дайджест.

Два AI-аналитика с противоположными методологиями (Oracle и Guardian)
разбирают один рыночный сигнал дня, фиксируют взаимоисключающие
проверяемые прогнозы и ведут публичный счёт точности в ledger.csv.

Запускается ежедневно из GitHub Actions, без серверов.
Режим DRY_RUN=1 — полный прогон без отправки в Telegram,
леджер пишется в ledger_dry.csv.
"""

import csv
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
LEDGER_FILE = os.path.join(ROOT, "ledger_dry.csv" if DRY_RUN else "ledger.csv")
PROMPTS_DIR = os.path.join(ROOT, "prompts")

MODEL_CLASSIFIER = "claude-haiku-4-5-20251001"
MODEL_DEBATE = "claude-sonnet-4-6"

HTTP_TIMEOUT = 15
PRICE_SOURCE = "binance_btcusdt_1d_close"
NEWS_URL = "https://t.me/s/markettwits"
REPO_URL = os.environ.get("REPO_URL", "https://github.com/AlexandreMortreux/the-room")
# прямая ссылка на файл леджера (можно переопределить через LEDGER_URL)
LEDGER_URL = os.environ.get("LEDGER_URL", f"{REPO_URL}/blob/main/ledger.csv")
DISCLAIMER = (
    "<i>Не является инвестиционной рекомендацией. "
    f'Прогнозы — эксперимент, <a href="{LEDGER_URL}">открытый леджер</a>.</i>'
)

LEDGER_FIELDS = [
    "id", "created_utc", "agent", "asset", "direction", "level",
    "horizon_h", "confidence", "price_source", "price_at_call",
    "expires_utc", "resolved_utc", "price_at_expiry", "result", "brier",
]

EVENT_CLASSES = {"macro", "flows", "regulation", "adoption", "incident"}


def log(msg):
    print(f"[the-room] {msg}", flush=True)


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
# HTTP: таймаут 15с, один ретрай; None при недоступности источника
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
# Дата-пейлоад: Binance spot/futures + Fear & Greed, все без ключей
# ---------------------------------------------------------------------------

# data-api.binance.vision — официальное зеркало рыночных данных Binance без
# геоблокировки: api.binance.com отдаёт 451 с IP GitHub Actions (США)
SPOT_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")


def fetch_klines():
    """Дневные свечи BTCUSDT: [open_time, o, h, l, close, vol, close_time, ...]."""
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

    # фьючерсные данные: Binance fapi geo-блокирует IP GitHub Actions,
    # поэтому запасной источник — публичные эндпоинты OKX (BTC-USDT-SWAP)
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

    fng = http_get_json("https://api.alternative.me/fng/", params={"limit": 7})
    if fng and fng.get("data"):
        payload["fear_greed_7d"] = [
            {
                "date": datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc).strftime("%Y-%m-%d"),
                "value": int(d["value"]),
                "classification": d["value_classification"],
            }
            for d in fng["data"]
        ]
    else:
        payload["fear_greed_7d"] = "unavailable"

    # Производные числа считаем здесь, а не в модели: 7-дневный лой/хай,
    # изменения в %, funding в % — готовыми, чтобы модель только цитировала.
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
# Новости: t.me/s/markettwits за последние 24 часа
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
            # permalink на конкретный пост — код запомнит его и подставит сам,
            # модели URL не отдаём (она их искажает)
            link_el = block.select_one("a.tgme_widget_message_date[href]")
            news.append({
                "datetime": iso(posted),
                "text": text[:600],
                "url": link_el["href"] if link_el else None,
            })
    except Exception as e:
        log(f"news parse failed, continuing with empty list: {e}")
        return []
    return news[-50:]


# ---------------------------------------------------------------------------
# Леджер
# ---------------------------------------------------------------------------

def load_ledger():
    if DRY_RUN and not os.path.exists(LEDGER_FILE):
        real = os.path.join(ROOT, "ledger.csv")
        if os.path.exists(real):
            shutil.copyfile(real, LEDGER_FILE)
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
        if r["agent"] not in score or r["result"] not in ("win", "loss"):
            continue
        s = score[r["agent"]]
        s["wins" if r["result"] == "win" else "losses"] += 1
        s["resolved"] += 1
        if r["brier"]:
            s["brier_sum"] += float(r["brier"])
    return score


# ---------------------------------------------------------------------------
# Шаг 1: резолв истёкших прогнозов по дневному закрытию Binance
# ---------------------------------------------------------------------------

def resolve_pending(rows, klines, now):
    """Проставляет win/loss и Brier по ближайшему дневному закрытию после expires_utc."""
    if not klines:
        log("klines unavailable, resolution skipped")
        return []
    finished = [k for k in klines if k[6] / 1000 <= now.timestamp()]
    resolved = []
    for row in rows:
        if row["result"] != "pending":
            continue
        expires = parse_iso(row["expires_utc"])
        if expires > now:
            continue
        candle = next((k for k in finished if k[6] / 1000 >= expires.timestamp()), None)
        if candle is None:
            continue  # дневная свеча ещё не закрылась — остаётся pending
        close = float(candle[4])
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


def build_resolution_post(resolved, rows):
    close = float(resolved[0]["price_at_expiry"])
    score = season_score(rows)
    winners = [r["agent"] for r in resolved if r["result"] == "win"]
    if winners:
        names = {"oracle": "🔮 Oracle", "guardian": "🛡 Guardian"}
        point_line = "Очко забирает " + " и ".join(names[w] for w in winners) + "."
    else:
        point_line = "Очко не забрал никто — оба мимо."

    lines = [f"🏁 <b>Развязка</b>: дневное закрытие BTC — <b>${close:,.0f}</b>", ""]
    for r in resolved:
        emoji = "🔮" if r["agent"] == "oracle" else "🛡"
        arrow = "выше" if r["direction"] == "above" else "ниже"
        mark = "✅" if r["result"] == "win" else "❌"
        lines.append(
            f"{emoji} {r['agent'].capitalize()}: {arrow} ${float(r['level']):,.0f} "
            f"(conf {float(r['confidence']):.2f}) — {mark} {r['result']}"
        )
    o, g = score["oracle"], score["guardian"]
    lines += [
        "",
        point_line,
        "",
        f"Счёт сезона: 🔮 {o['wins']}W-{o['losses']}L | 🛡 {g['wins']}W-{g['losses']}L",
        f"Brier (накопл.): 🔮 {o['brier_sum']:.3f} | 🛡 {g['brier_sum']:.3f}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Track records за последние 14 дней
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


# ---------------------------------------------------------------------------
# Claude: вызовы со строгим JSON, один ретрай с текстом ошибки
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


def call_claude_json(client, model, system, user, max_tokens, validate):
    messages = [{"role": "user", "content": user}]
    last_err = None
    for attempt in range(2):
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
            log(f"model JSON invalid (attempt {attempt + 1}): {e}")
            messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        f"Твой ответ невалиден: {e}. "
                        "Верни исправленный строгий JSON без preamble и markdown."
                    ),
                },
            ]
    raise RuntimeError(f"model returned invalid JSON after retry: {last_err}")


def validate_classifier(data):
    signals = data["signals"]
    if not isinstance(signals, list):
        raise ValueError("'signals' must be a list")
    for s in signals:
        for key in ("headline", "event_class", "horizon_days", "priced_in_note",
                    "why_now", "source_id"):
            if key not in s:
                raise ValueError(f"signal missing key '{key}'")
        if s["event_class"] not in EVENT_CLASSES:
            raise ValueError(f"bad event_class: {s['event_class']}")
        if not isinstance(s["source_id"], int):
            raise ValueError("'source_id' must be an integer (id входной новости)")


def classify_news(client, news):
    if not news:
        return []
    # модели отдаём индексированный список без URL; ссылку возьмём из кода по source_id
    indexed = [
        {"id": i, "datetime": n["datetime"], "text": n["text"]}
        for i, n in enumerate(news)
    ]
    user = "Новости за последние 24 часа:\n" + json.dumps(indexed, ensure_ascii=False, indent=1)
    data = call_claude_json(
        client, MODEL_CLASSIFIER, read_prompt("classifier.txt"),
        user, max_tokens=1000, validate=validate_classifier,
    )
    signals = data["signals"][:2]
    for s in signals:
        sid = s.get("source_id")
        s["source_url"] = news[sid]["url"] if isinstance(sid, int) and 0 <= sid < len(news) else None
    return signals


def make_debate_validator(current_price):
    def validate(data):
        if not isinstance(data.get("post_html"), str) or not data["post_html"].strip():
            raise ValueError("'post_html' must be a non-empty string")
        preds = data["predictions"]
        if not isinstance(preds, list) or len(preds) != 2:
            raise ValueError("'predictions' must contain exactly 2 items")
        if {p.get("agent") for p in preds} != {"oracle", "guardian"}:
            raise ValueError("agents must be exactly 'oracle' and 'guardian'")
        if {p.get("direction") for p in preds} != {"above", "below"}:
            raise ValueError("directions must be mutually exclusive: one 'above', one 'below'")
        levels = {round(float(p["level"]), 2) for p in preds}
        if len(levels) != 1:
            raise ValueError("both predictions must reference the same level")
        for p in preds:
            if p.get("asset") != "BTC":
                raise ValueError("asset must be 'BTC'")
            if int(p["horizon_h"]) != 24:
                raise ValueError("horizon_h must be 24")
            conf = float(p["confidence"])
            if not 0.55 <= conf <= 0.80:
                raise ValueError(f"confidence {conf} outside [0.55, 0.80]")
            level = float(p["level"])
            if abs(level - current_price) > 0.15 * current_price:
                raise ValueError(
                    f"level {level} outside ±15% of current price {current_price}"
                )
    return validate


def run_debate(client, signal, data_payload, track_records, current_price):
    template = "orchestrator.txt" if signal else "fallback.txt"
    system = read_prompt("oracle.txt") + "\n\n" + read_prompt("guardian.txt")
    inputs = {
        "signal": signal,
        "data_payload": data_payload,
        "track_records": track_records,
    }
    user = read_prompt(template) + "\n\nВходные данные:\n" + json.dumps(
        inputs, ensure_ascii=False, indent=1
    )
    return call_claude_json(
        client, MODEL_DEBATE, system, user,
        max_tokens=2000, validate=make_debate_validator(current_price),
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_call(method, payload):
    if DRY_RUN:
        print(f"\n[DRY_RUN] Telegram {method}:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    token = os.environ["TG_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            body = resp.json()
            if body.get("ok"):
                return
            last_err = body.get("description", resp.text)
        except (requests.RequestException, ValueError) as e:
            last_err = e
        if attempt == 0:
            time.sleep(3)
    raise RuntimeError(f"Telegram {method} failed: {last_err}")


def tg_send_message(html):
    tg_call("sendMessage", {
        "chat_id": os.environ.get("TG_CHANNEL_ID", ""),
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def tg_send_poll(predictions):
    by_agent = {p["agent"]: p for p in predictions}
    level = float(by_agent["oracle"]["level"])

    def option(agent, emoji, name):
        arrow = "выше" if by_agent[agent]["direction"] == "above" else "ниже"
        return f"{emoji} {name} — {arrow} ${level:,.0f}"

    tg_call("sendPoll", {
        "chat_id": os.environ.get("TG_CHANNEL_ID", ""),
        "question": "🎯 Кто прав завтра?",
        "options": [option("oracle", "🔮", "Oracle"), option("guardian", "🛡", "Guardian")],
        "is_anonymous": True,
    })


# ---------------------------------------------------------------------------
# Воскресный scorecard (по UTC+8)
# ---------------------------------------------------------------------------

def is_sunday_utc8(now):
    return (now + timedelta(hours=8)).weekday() == 6


def build_scorecard_stats(rows, now):
    resolved = [r for r in rows if r["result"] in ("win", "loss")]
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
    stats = {"season": {}, "week": {}}
    for agent in ("oracle", "guardian"):
        s = score[agent]
        stats["season"][agent] = {
            "wins": s["wins"],
            "losses": s["losses"],
            "accuracy_pct": round(100 * s["wins"] / s["resolved"], 1) if s["resolved"] else None,
            "cumulative_brier": round(s["brier_sum"], 4),
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
        "Ты — редактор ежедневного BTC-дайджеста THE ROOM. По данным ниже собери "
        "воскресный scorecard-пост: заголовок «🏆 Итоги недели», W/L сезона обоих агентов "
        "(🔮 Oracle и 🛡 Guardian), точность %, накопленный Brier, текущая серия, "
        "лучший и худший колл недели с фактами. Сухо, с лёгкой иронией, ≤20 секунд чтения. "
        "Формат Telegram HTML (<b>, <i>), без markdown. "
        "Выход — только текст поста, без preamble."
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
# Основной прогон
# ---------------------------------------------------------------------------

def render_source(post_html, signal):
    """Подставляет на место {{SOURCE}} объяснение выбора (why_now, курсивом)
    и кликабельную ссылку на первоисточник. URL берётся из кода, не из модели."""
    post = post_html.strip()
    parts = []
    if signal and signal.get("why_now"):
        parts.append(f"<i>Почему сегодня: {signal['why_now']}</i>")
    if signal and signal.get("source_url"):
        parts.append(f'📎 <a href="{signal["source_url"]}">Источник: MarketTwits</a>')
    block = "\n".join(parts)
    if "{{SOURCE}}" in post:
        return post.replace("{{SOURCE}}", block).strip()
    return (post + ("\n\n" + block if block else "")).strip()


def append_predictions(rows, predictions, current_price, now):
    expires = now + timedelta(hours=24)
    for p in predictions:
        rows.append({
            "id": f"{now:%Y%m%d-%H%M}-{p['agent']}",
            "created_utc": iso(now),
            "agent": p["agent"],
            "asset": "BTC",
            "direction": p["direction"],
            "level": f"{float(p['level']):.2f}",
            "horizon_h": "24",
            "confidence": f"{float(p['confidence']):.2f}",
            "price_source": PRICE_SOURCE,
            "price_at_call": f"{current_price:.2f}",
            "expires_utc": iso(expires),
            "resolved_utc": "",
            "price_at_expiry": "",
            "result": "pending",
            "brier": "",
        })


def main():
    now = utcnow()
    log(f"run started {iso(now)}, dry_run={DRY_RUN}")
    client = anthropic.Anthropic()

    # 1. Резолв истёкших прогнозов
    rows = load_ledger()
    klines = fetch_klines()
    current_price = fetch_current_price(klines)
    resolved = resolve_pending(rows, klines, now)
    if resolved:
        save_ledger(rows)
        log(f"resolved {len(resolved)} prediction(s)")
        # 2. Пост развязки
        tg_send_message(build_resolution_post(resolved, rows))

    # 3–4. Новости и дата-пейлоад
    news = fetch_news(now)
    log(f"news collected: {len(news)}")
    data_payload = build_data_payload(klines, current_price)

    # 5. Классификатор сигналов
    signals = classify_news(client, news)
    signal = signals[0] if signals else None
    log(f"signal of the day: {signal['headline'] if signal else 'none (fallback mode)'}")

    # 6. Track records за 14 дней
    track_records = build_track_records(rows, now)

    # 7. Дебат
    debate = run_debate(client, signal, data_payload, track_records, current_price)

    # 8. Публикация: пост (с источником) + опрос
    post_html = render_source(debate["post_html"], signal)
    tg_send_message(post_html + "\n\n" + DISCLAIMER)
    tg_send_poll(debate["predictions"])

    # 9. Запись прогнозов в леджер
    append_predictions(rows, debate["predictions"], current_price, now)
    save_ledger(rows)
    log("predictions appended to ledger")

    # 10. Воскресный scorecard (UTC+8)
    if is_sunday_utc8(now):
        post_scorecard(client, rows, now)

    log("run finished")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
