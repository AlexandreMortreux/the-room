#!/usr/bin/env python3
"""Fast, dependency-light regression tests for THE ROOM's counters and resolver.

Run: python tests.py   (exit 0 = all passed). No network, no Telegram, no Claude.
These lock the behaviour that broke in the Case 10 double-resolve incident plus
the Case/Day counter robustness across a missing day (the Jul 14 hole)."""
import os
from datetime import datetime, timezone, timedelta

os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("TG_BOT_TOKEN", "x")
os.environ.setdefault("TG_CHANNEL_ID", "@x")

import main

main.alert_owner = lambda text: _ALERTS.append(text)  # never hit Telegram
_ALERTS = []


def candle(y, mo, d, close):
    """A daily kline: open_time at 00:00 UTC of the day, close_time ~ next 00:00."""
    o = int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp() * 1000)
    return [o, 0, 0, 0, close, 0, o + 86400000 - 1, 0, 0, 0, 0, 0]


def pair(created_iso, level, horizon="24"):
    return [
        {"created_utc": created_iso, "agent": a, "asset": "BTC", "direction": d,
         "level": str(level), "horizon_h": horizon, "confidence": "0.60",
         "expires_utc": created_iso, "resolved_utc": "", "price_at_expiry": "",
         "result": "pending", "brier": ""}
        for a, d in (("oracle", "above"), ("guardian", "below"))
    ]


def check(name, cond):
    print(f"  {'ok ' if cond else 'FAIL'}  {name}")
    assert cond, name


def test_resolve_close_date():
    # a 24h pair created day D settles on D+1's close; a weekly on D+7
    d = main.resolve_close_date("2026-07-16T03:42:19+00:00", "24")
    check("24h pair created Jul 16 -> resolves Jul 17 candle", d.isoformat() == "2026-07-17")
    w = main.resolve_close_date("2026-07-11T04:00:00+00:00", "168")
    check("weekly created Jul 11 -> resolves Jul 18 candle", w.isoformat() == "2026-07-18")


def test_pair_resolves_against_its_own_dplus1():
    klines = [candle(2026, 7, d, 60000 + d) for d in range(14, 18)]  # Jul 14..17 finished
    rows = pair("2026-07-16T03:42:00+00:00", 65044)  # Case 10; D+1 = Jul 17 candle (close 60017)
    # before the Jul 17 close prints -> stays pending
    res = main.resolve_pending([dict(r) for r in rows],
                               [candle(2026, 7, d, 60000 + d) for d in range(14, 17)],  # up to Jul 16
                               datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc))
    check("pending until its own D+1 close prints", res == [])
    # once Jul 17 close exists -> resolves against IT (60017), not Jul 16 (60016)
    res = main.resolve_pending(rows, klines, datetime(2026, 7, 18, 0, 25, tzinfo=timezone.utc))
    check("resolves against the Jul 17 close, not Jul 16",
          all(r["price_at_expiry"] == "60017.00" for r in res) and len(res) == 2)


def test_sunday_daily_resolves_despite_weekly():
    # a Sunday run posts a weekly (168h) and a daily (24h) at the SAME created_utc;
    # the daily must still resolve against its own D+1 close, not the weekly's D+7
    created = "2026-07-19T03:58:00+00:00"
    daily = pair(created, 64834)                    # D+1 = Jul 20 candle
    weekly = pair(created, 65044, horizon="168")    # D+7 = Jul 26 candle
    klines = [candle(2026, 7, d, 60000 + d) for d in range(15, 21)]  # ... Jul 20 closed
    res = main.resolve_pending(daily + weekly, klines,
                               datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc))
    check("Sunday daily resolves (not merged with the weekly)",
          len(res) == 2 and all(r["horizon_h"] == "24" for r in res))
    check("weekly stays pending (its D+7 close hasn't printed)",
          all(r["result"] == "pending" for r in weekly))


def test_one_close_one_pair_invariant():
    _ALERTS.clear()
    klines = [candle(2026, 7, d, 60000 + d) for d in range(14, 17)]  # Jul 16 close available
    # two DISTINCT pending pairs both created Jul 15 -> both map to the Jul 16 close
    rows = pair("2026-07-15T03:00:00+00:00", 63000) + pair("2026-07-15T09:00:00+00:00", 63500)
    res = main.resolve_pending(rows, klines, datetime(2026, 7, 17, 2, 0, tzinfo=timezone.utc))
    check("two pairs on one close -> resolves NEITHER", res == [])
    check("all four rows stay pending", all(r["result"] == "pending" for r in rows))
    check("owner alerted on the clash", len(_ALERTS) >= 1)


def test_counters_survive_a_missing_day():
    # pairs for Jul 6..8 then a HOLE on Jul 9, resuming Jul 10 (mirrors the Jul 14 gap)
    rows = []
    for day in (6, 7, 8, 10, 11):
        rows += pair(f"2026-07-{day:02d}T04:00:00+00:00", 63000)
    # Case# is a sequential pair index — no gap, no shift across the hole
    cases = [main.case_number(rows, f"2026-07-{day:02d}T04:00:00+00:00") for day in (6, 7, 8, 10, 11)]
    check("Case# stays sequential across the hole (1..5, no jump)", cases == [1, 2, 3, 4, 5])
    # Day# is calendar-based from DAY0 (2026-07-06) — the hole day still counts
    days = [main.day_number(datetime(2026, 7, day, tzinfo=timezone.utc)) for day in (6, 7, 8, 10, 11)]
    check("Day# is calendar days from DAY0 (hole day counted)", days == [1, 2, 3, 5, 6])


def test_quant_claims_consistency():
    p = {"extreme_fear_streak_days": 5, "funding_rate_pct": 0.0051,
         "weekly_change_pct": 2.26, "open_interest_btc": 30377.0}
    # a drifted count is flagged (the 'seven' vs 'five' incident)
    check("wrong streak claim warns",
          main.check_quant_claims("<b>Seven straight sessions</b> of extreme fear.", p))
    # the correct count and correctly-cited figures produce no warning
    clean = ("Five consecutive days of fear, funding 0.0051%, up 2.26% on the "
             "week, OI 30,377 BTC.")
    check("consistent post -> no warning", main.check_quant_claims(clean, p) == [])
    # rounded/reworded figures are flagged
    check("funding drift warns", main.check_quant_claims("Funding at 0.02% now.", p))
    check("weekly drift warns", main.check_quant_claims("Up 3.10% on the week.", p))


def test_debate_validator_limits():
    from datetime import date as _date
    v = main.make_debate_validator(64000.0, [64000.0], 11, _date(2026, 7, 20))
    good = {
        "setup": "Oil spikes on the strike; BTC hovers just under the line.",
        "oracle_open": "Called it wrong yesterday, but the fear index has been pinned at extreme for five days and the flush never came. Sellers are exhausted; this floor holds.",
        "guardian_attack": "Exhausted sellers? Five days of extreme fear and no bounce is the tell — that is distribution, not a floor. Every failed rally adds supply overhead. It cracks.",
        "oracle_jab": "Overhead supply is just fear with a fancy name. Buyers step in where others flinch.",
        "card_caption": "Fear says floor, price says otherwise.",
        "predictions": [
            {"agent": "oracle", "asset": "BTC", "direction": "above", "level": 65044, "horizon_h": 24, "confidence": 0.62, "driver": "Fear at an extreme"},
            {"agent": "guardian", "asset": "BTC", "direction": "below", "level": 65044, "horizon_h": 24, "confidence": 0.7, "driver": "Rallies keep failing"},
        ],
    }
    v(good)  # must not raise
    check("valid debate passes", True)

    def raises(mut):
        d = {**good, **mut}
        try:
            v(d); return False
        except ValueError:
            return True
    check("$ in a reply message fails", raises({"oracle_jab": "Buyers defend $65,044 every time."}))
    check("% in a reply message fails", raises({"oracle_jab": "Buyers win 60% of these."}))
    check("promise word 'will' in a reply fails", raises({"oracle_jab": "This floor will hold, guaranteed."}))
    check("over-long oracle_jab fails", raises({"oracle_jab": "x" * 200}))


def test_x_post_texts():
    from datetime import date as _date
    rows = pair("2026-07-20T04:00:00+00:00", 65044)  # one pending pair -> score 0:0
    preds = [{"agent": "oracle", "level": "65044", "direction": "above"},
             {"agent": "guardian", "level": "65044", "direction": "below"}]
    morning = main.build_x_morning(9, preds, rows, _date(2026, 7, 21))
    check("morning notarial: Case/Line/Score/Resolves present",
          morning.startswith("Case 9. Oracle says up, Guardian says down.")
          and "Line $65,044" in morning and "Resolves Jul 21 close." in morning)
    check("morning is not Alexandre-lowercase", morning != morning.lower())
    win = main.build_x_evening(9, "guardian", rows, "http://x/led.csv")
    check("evening: point + score + ledger link",
          "Point Guardian." in win and "Ledger: http://x/led.csv" in win)
    check("evening: no-point when nobody wins", "No point." in main.build_x_evening(9, None, rows, "u"))


if __name__ == "__main__":
    for t in (test_resolve_close_date, test_pair_resolves_against_its_own_dplus1,
              test_sunday_daily_resolves_despite_weekly,
              test_one_close_one_pair_invariant, test_counters_survive_a_missing_day,
              test_quant_claims_consistency, test_debate_validator_limits,
              test_x_post_texts):
        print(t.__name__)
        t()
    print("\nAll tests passed.")
