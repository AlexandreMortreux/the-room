#!/usr/bin/env python3
"""THE ROOM daily card — PNG for sendPhoto. 1080x1080, flat editorial.

Shows the current market, today's watershed level with both agents' bets, and
(when a prediction resolved this run) yesterday's close as a win-coloured marker
plus a footer line. Visual tokens are frozen; do not restyle.
"""
import colorsys, requests, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle
from matplotlib import font_manager
from datetime import datetime, timezone

BG      = "#0A0A0A"
TEXT    = "#F2F0EB"
ORACLE  = "#A78BFA"
GUARDIAN= "#F2B544"
WIN     = "#4FCE7F"
LOSS    = "#EF6A6A"
MUTED   = "#6B7280"

_INSTALLED = {f.name for f in font_manager.fontManager.ttflist}

def _tabular(fam):
    f = plt.figure(); a = f.text(0, 0, "0", fontfamily=fam); b = f.text(0, 0, "1", fontfamily=fam)
    f.canvas.draw(); r = f.canvas.get_renderer()
    ok = abs(a.get_window_extent(r).width - b.get_window_extent(r).width) < 0.5
    plt.close(f); return ok

HEAD = next((c for c in ["Inter Tight", "Inter", "DejaVu Sans"] if c in _INSTALLED), "DejaVu Sans")
NUM  = next((c for c in ["Inter Tight", "Inter", "DejaVu Sans"]
             if c in _INSTALLED and _tabular(c)), "DejaVu Sans")
plt.rcParams["font.family"] = HEAD

def _track(s):
    return " ".join(s)

def _at_light(hex_c, l):
    r, g, b = (int(hex_c[i:i+2], 16) / 255 for i in (1, 3, 5))
    h, _, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r*255), round(g*255), round(b*255))

def _wfrac(t, fig):
    return t.get_window_extent(fig.canvas.get_renderer()).width / fig.bbox.width

def load_closes(n=14):
    r = requests.get("https://data-api.binance.vision/api/v3/klines",
                     params={"symbol": "BTCUSDT", "interval": "1d", "limit": n + 1}, timeout=15)
    r.raise_for_status()
    return [float(k[4]) for k in r.json()][-n:]

AX = [0.06, 0.375, 0.88, 0.245]

def build_card(out, price, preds, level, season, resolved=None, closes=None):
    """resolved = {'winner': 'oracle'|'guardian'|None, 'close_px': float} or None."""
    if closes is None:
        closes = load_closes(14)
    day_chg = (closes[-1] / closes[-2] - 1) * 100
    fig = plt.figure(figsize=(10.8, 10.8), dpi=100, facecolor=BG)

    # header
    fig.text(0.06, 0.955, _track("THE ROOM"), color=MUTED, fontsize=15, va="center")
    fig.text(0.94, 0.955, _track(datetime.now(timezone.utc).strftime("%d %b %Y").upper()),
             color=MUTED, fontsize=15, ha="right", va="center")

    # hero + day change with arrow
    hero = fig.text(0.055, 0.815, f"${price:,.0f}", color=TEXT, fontsize=158,
                    fontweight="bold", va="center", fontfamily=NUM)
    fig.text(0.062, 0.700, f"{'↑' if day_chg >= 0 else '↓'} {day_chg:+.2f}% today",
             color=(WIN if day_chg >= 0 else LOSS), fontsize=26, va="center", fontfamily=NUM)

    # chart — transparent axes, full-bleed zone fills split at the level
    ax = fig.add_axes(AX); ax.patch.set_visible(False)
    disp = list(closes)
    if resolved and resolved.get("close_px"):
        disp[-1] = resolved["close_px"]
    x = list(range(len(disp)))
    lo, hi = min(min(disp), level), max(max(disp), level)
    pad = (hi - lo) * 0.14 or hi * 0.01
    lo, hi = lo - pad, hi + pad
    ax.set_ylim(lo, hi); ax.set_xlim(-0.4, len(disp) - 0.6)
    fy = AX[1] + AX[3] * (level - lo) / (hi - lo)
    fig.patches.append(Rectangle((0, fy), 1, AX[1] + AX[3] - fy, transform=fig.transFigure,
                                 facecolor=ORACLE, alpha=0.08, edgecolor="none", zorder=0))
    fig.patches.append(Rectangle((0, AX[1]), 1, fy - AX[1], transform=fig.transFigure,
                                 facecolor=GUARDIAN, alpha=0.08, edgecolor="none", zorder=0))
    ax.axhline(level, color=TEXT, ls=(0, (5, 4)), lw=1.2, alpha=0.5, zorder=2)
    ax.annotate(f"${level:,.0f}", xy=(len(disp) - 0.6, level), xytext=(0, 8),
                textcoords="offset points", ha="right", va="bottom", color=TEXT,
                fontsize=15, fontweight="bold", zorder=4, fontfamily=NUM,
                bbox=dict(boxstyle="square,pad=0.25", fc=BG, ec="none", alpha=0.85))
    ax.plot(x, disp, color=TEXT, lw=3, solid_capstyle="round", solid_joinstyle="round", zorder=3)
    if resolved and resolved.get("close_px"):
        ax.plot([x[-1]], [resolved["close_px"]], marker="o", ms=15, color=WIN,
                markeredgecolor=BG, markeredgewidth=2.5, zorder=6)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)

    # bet chips — triangle before ABOVE/BELOW, bold tabular percent, auto-fit
    chip_txts = []
    def chip(cx0, w, color, name, pct):
        y, h = 0.235, 0.090
        fig.patches.append(FancyBboxPatch(
            (cx0, y), w, h, boxstyle="round,pad=0,rounding_size=0.01",
            transform=fig.transFigure, facecolor=_at_light(color, 0.18),
            edgecolor="none", linewidth=0, zorder=1))
        cy = y + h/2
        tn = fig.text(0, cy, name, color=TEXT, fontsize=23, va="center", zorder=3)
        tp = fig.text(0, cy, pct, color=TEXT, fontsize=25, fontweight="bold", va="center",
                      zorder=3, fontfamily=NUM)
        chip_txts.append([tn, tp, cx0 + w/2, w])
    chip(0.06, 0.42, ORACLE, "ORACLE · ▲ ABOVE · ", f"{round(preds['oracle']*100)}%")
    chip(0.52, 0.42, GUARDIAN, "GUARDIAN · ▼ BELOW · ", f"{round(preds['guardian']*100)}%")

    # footer
    s0, s1 = season
    if resolved:
        w = resolved.get("winner")
        who = ({"oracle": "ORACLE", "guardian": "GUARDIAN"}.get(w, "SPLIT"))
        tick = " ✓" if w else ""
        foot = (f"Yesterday:  {who}{tick}  closed ${resolved['close_px']:,.0f}"
                f"       Season:  Oracle {s0}  |  Guardian {s1}")
    else:
        foot = f"Season:  Oracle {s0}  |  Guardian {s1}"
    fig.text(0.06, 0.095, foot, color=MUTED, fontsize=15, va="center")

    # measure pass: hero fit + chips inside their boxes, then centered
    fig.canvas.draw()
    if _wfrac(hero, fig) > 0.90:
        hero.set_fontsize(158 * 0.90 / _wfrac(hero, fig))
    for tn, tp, cx, w in chip_txts:
        total = _wfrac(tn, fig) + _wfrac(tp, fig)
        if total > w - 0.035:
            k = (w - 0.035) / total
            tn.set_fontsize(tn.get_fontsize() * k); tp.set_fontsize(tp.get_fontsize() * k)
    fig.canvas.draw()
    for tn, tp, cx, w in chip_txts:
        wn, wp = _wfrac(tn, fig), _wfrac(tp, fig)
        sx = cx - (wn + wp) / 2
        tn.set_position((sx, tn.get_position()[1]))
        tp.set_position((sx + wn, tp.get_position()[1]))

    fig.savefig(out, facecolor=BG)
    plt.close(fig)
    return out
