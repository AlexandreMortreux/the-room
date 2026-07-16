#!/usr/bin/env python3
"""THE ROOM bet card v2 — fills templates/card-v2.svg by id and renders a PNG.

Text is drawn with the bundled Inter Tight SemiBold only: fontconfig is pointed
at assets/fonts and nothing else, so a missing font fails loudly instead of
silently falling back to a system face.
"""
import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "templates" / "card-v2.svg"
FONT = ROOT / "assets" / "fonts" / "InterTight-SemiBold.ttf"
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

DGREEN = "#41E47D"   # direction: up / positive change
DRED = "#FF383C"     # direction: down / negative change
REASON_SIZE = 24     # font-size of the reason rows in the template
REASON_MAX_PX = 1014 - 589 - 16   # reason x -> panel right edge, minus padding
REASON_LINES = 3     # the template has 3 reason rows per side


# ---------------------------------------------------------------------------
# formatters
# ---------------------------------------------------------------------------

def money(v):
    return f"${float(v):,.0f}"


def fmt_bet(direction, level):
    return f"{str(direction).upper()} {money(level)}"


def fmt_conviction(conf, bucket):
    return f"{round(float(conf) * 100)}% {bucket}"


def fmt_change(pct):
    return f"{float(pct):+.2f}%"


def fmt_day_score(day_n, oracle_wins, guardian_wins):
    return f"Day {day_n} : Oracle {oracle_wins} : {guardian_wins} Guardian"


def fmt_last_result(winner):
    """winner: 'oracle' | 'guardian' | None (no resolution that day)."""
    if winner in ("oracle", "guardian"):
        return f"Yesterday: point {winner.capitalize()}"
    return "Yesterday: —"


# ---------------------------------------------------------------------------
# font + template plumbing
# ---------------------------------------------------------------------------

def _ensure_font():
    """Point fontconfig at the bundled font dir only. Must run before cairosvg
    is imported, so cairo's fontconfig picks up the env."""
    if not FONT.exists():
        raise RuntimeError(
            f"Inter Tight SemiBold missing at {FONT} — refusing to render "
            f"with a system fallback font")
    conf_dir = Path(tempfile.gettempdir()) / "theroom-fontconfig"
    (conf_dir / "cache").mkdir(parents=True, exist_ok=True)
    conf = conf_dir / "fonts.conf"
    conf.write_text(
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        "<fontconfig>\n"
        f"  <dir>{FONT.parent}</dir>\n"
        f'  <cachedir>{conf_dir / "cache"}</cachedir>\n'
        "</fontconfig>\n")
    os.environ["FONTCONFIG_FILE"] = str(conf)


def _find(root, el_id):
    el = root.find(f".//*[@id='{el_id}']")
    if el is None:
        raise KeyError(f"template id not found: {el_id}")
    return el


def _set_text(root, el_id, value):
    """Set the first <tspan> under an id — works whether the id sits on the
    <text> itself or on a <g> wrapper (as it does for the reason rows)."""
    tspan = _find(root, el_id).find(f".//{{{SVG_NS}}}tspan")
    if tspan is None:
        raise KeyError(f"no <tspan> under {el_id}")
    tspan.text = value


def _set_fill(root, el_id, color):
    _find(root, el_id).set("fill", color)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

_FONT_METRICS = None


def text_width_px(s, size=REASON_SIZE):
    """Rendered advance width of s in the bundled font, in px at `size`."""
    global _FONT_METRICS
    if _FONT_METRICS is None:
        from fontTools.ttLib import TTFont
        f = TTFont(FONT, lazy=True)
        _FONT_METRICS = (f.getBestCmap(), f["hmtx"].metrics, f["head"].unitsPerEm)
    cmap, hmtx, upem = _FONT_METRICS
    total = 0
    for ch in s:
        g = cmap.get(ord(ch))
        total += hmtx[g][0] if g in hmtx else hmtx[".notdef"][0]
    return total * size / upem


def _ellipsize(s, max_px, size):
    """Trim s so that s + '…' fits within max_px."""
    if text_width_px(s + "…", size) <= max_px:
        return s + "…"
    while s and text_width_px(s.rstrip() + "…", size) > max_px:
        s = s[:-1]
    return (s.rstrip() + "…") if s.strip() else "…"


def wrap_reason(text, max_px=REASON_MAX_PX, size=REASON_SIZE, max_lines=REASON_LINES):
    """Word-wrap a reason to at most max_lines by measured width; if the text
    still doesn't fit, the last kept line ends with an ellipsis. Returns exactly
    max_lines strings (padded with '')."""
    words = (text or "").strip().split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if text_width_px(trial, size) <= max_px:
            cur = trial
            continue
        # w doesn't start a new line unless there's room; else we're out of space
        if len(lines) + 1 >= max_lines and cur:
            lines.append(_ellipsize(cur, max_px, size))
            return (lines + [""] * max_lines)[:max_lines]
        if cur:
            lines.append(cur)
        cur = w if text_width_px(w, size) <= max_px else _ellipsize(w, max_px, size)
    if cur:
        lines.append(cur)
    return (lines + [""] * max_lines)[:max_lines]


def validate(bull_reason, bear_reason):
    if bull_reason.strip().lower() == bear_reason.strip().lower():
        raise ValueError("bull and bear reasons are identical — the two sides must differ")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def build_card_v2(out, *, case_no, date_label, day_n, oracle_wins, guardian_wins,
                  last_winner, level, btc_price, btc_change, resolve_label,
                  bull_conf, bull_bucket, bull_reason,
                  bear_conf, bear_bucket, bear_reason):
    """Render the v2 bet card. Each side's reason is one plain sentence; it is
    word-wrapped to the 3 reason rows and ellipsised if it overflows."""
    validate(bull_reason, bear_reason)
    bull_lines = wrap_reason(bull_reason)
    bear_lines = wrap_reason(bear_reason)

    _ensure_font()
    import cairosvg  # imported after FONTCONFIG_FILE is set

    tree = ET.parse(TEMPLATE)
    root = tree.getroot()

    _set_text(root, "t:case_no", f"Case no.{case_no}")
    _set_text(root, "t:date", date_label)
    _set_text(root, "t:day_score", fmt_day_score(day_n, oracle_wins, guardian_wins))
    _set_text(root, "t:last_result", fmt_last_result(last_winner))

    _set_text(root, "t:bull_conviction", fmt_conviction(bull_conf, bull_bucket))
    _set_text(root, "t:bull_bet", fmt_bet("above", level))
    _set_text(root, "t:bear_conviction", fmt_conviction(bear_conf, bear_bucket))
    _set_text(root, "t:bear_bet", fmt_bet("below", level))
    for i in range(3):
        _set_text(root, f"t:bull_reason_{i+1}", bull_lines[i])
        _set_text(root, f"t:bear_reason_{i+1}", bear_lines[i])

    _set_text(root, "t:watershed", money(level))
    _set_text(root, "t:btc_price", money(btc_price))
    _set_text(root, "t:btc_change", fmt_change(btc_change))
    _set_fill(root, "t:btc_change", DGREEN if float(btc_change) >= 0 else DRED)
    _set_text(root, "t:resolve_time", resolve_label)

    cairosvg.svg2png(bytestring=ET.tostring(root, encoding="utf-8"),
                     write_to=str(out), output_width=1080, output_height=1080)
    return out
