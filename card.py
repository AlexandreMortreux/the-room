#!/usr/bin/env python3
"""THE ROOM daily cards — PNGs for sendPhoto. 1080x1080, flat editorial.

Two templates sharing one visual system (tokens are frozen, do not restyle):
  Template A — build_resolution_card: morning resolution. Hero = daily close,
    explicit period line, winner chip emphasized (win border + check), loser
    chip dimmed, close marker on the chart, Season + streak footer.
  Template B — build_today_card: today's bets. Hero = current price with day
    change, watershed level, two equal bet chips, Season-only footer.
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

AX = [0.06, 0.375, 0.88, 0.245]   # chart axes rect
CHIP_Y, CHIP_H = 0.235, 0.090


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


def _header(fig, big=False):
    """Brand left, date right. Template A: +20% size, wider letterspacing."""
    size = 18 if big else 15
    gap = "  " if big else " "
    fig.text(0.06, 0.955, gap.join("THE ROOM"), color=MUTED, fontsize=size, va="center")
    fig.text(0.94, 0.955, " ".join(datetime.now(timezone.utc).strftime("%d %b %Y").upper()),
             color=MUTED, fontsize=size, ha="right", va="center")


def _chart(fig, closes, level, marker_px=None):
    """Shared line chart: zone fills split at the level, dashed level + label,
    optional win-coloured marker at the last point (set to marker_px)."""
    ax = fig.add_axes(AX); ax.patch.set_visible(False)
    disp = list(closes)
    if marker_px is not None:
        disp[-1] = marker_px
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
    if marker_px is not None:
        ax.plot([x[-1]], [marker_px], marker="o", ms=15, color=WIN,
                markeredgecolor=BG, markeredgewidth=2.5, zorder=6)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)


def _chip(fig, chips, x, w, color, name, pct, edge=None, dim=False, scale=1.0):
    fig.patches.append(FancyBboxPatch(
        (x, CHIP_Y), w, CHIP_H, boxstyle="round,pad=0,rounding_size=0.01",
        transform=fig.transFigure, facecolor=_at_light(color, 0.10 if dim else 0.18),
        edgecolor=(edge or "none"), linewidth=3 if edge else 0, zorder=1))
    cy = CHIP_Y + CHIP_H / 2
    col = MUTED if dim else TEXT
    tn = fig.text(0, cy, name, color=col, fontsize=23 * scale, va="center", zorder=3)
    tp = fig.text(0, cy, pct, color=col, fontsize=25 * scale, fontweight="bold",
                  va="center", zorder=3, fontfamily=NUM)
    chips.append([tn, tp, x + w / 2, w])


def _finish(fig, out, hero, chips):
    """Measure pass: auto-fit the hero, keep chip text inside its box, center it."""
    fig.canvas.draw()
    if _wfrac(hero, fig) > 0.90:
        hero.set_fontsize(hero.get_fontsize() * 0.90 / _wfrac(hero, fig))
    for tn, tp, cx, w in chips:
        total = _wfrac(tn, fig) + _wfrac(tp, fig)
        if total > w - 0.030:
            k = (w - 0.030) / total
            tn.set_fontsize(tn.get_fontsize() * k); tp.set_fontsize(tp.get_fontsize() * k)
    fig.canvas.draw()
    for tn, tp, cx, w in chips:
        wn, wp = _wfrac(tn, fig), _wfrac(tp, fig)
        sx = cx - (wn + wp) / 2
        tn.set_position((sx, tn.get_position()[1]))
        tp.set_position((sx + wn, tp.get_position()[1]))
    fig.savefig(out, facecolor=BG)
    plt.close(fig)
    return out


def build_today_card(out, price, preds, level, season, closes=None):
    """Template B — today's bets. No 'Yesterday' line: that lives in Template A."""
    if closes is None:
        closes = load_closes(14)
    day_chg = (closes[-1] / closes[-2] - 1) * 100
    fig = plt.figure(figsize=(10.8, 10.8), dpi=100, facecolor=BG)
    _header(fig)
    hero = fig.text(0.055, 0.815, f"${price:,.0f}", color=TEXT, fontsize=158,
                    fontweight="bold", va="center", fontfamily=NUM)
    fig.text(0.062, 0.700, f"{'↑' if day_chg >= 0 else '↓'} {day_chg:+.2f}% today",
             color=(WIN if day_chg >= 0 else LOSS), fontsize=26, va="center", fontfamily=NUM)
    _chart(fig, closes, level)
    chips = []
    _chip(fig, chips, 0.06, 0.42, ORACLE, "ORACLE · ▲ ABOVE · ", f"{round(preds['oracle']*100)}%")
    _chip(fig, chips, 0.52, 0.42, GUARDIAN, "GUARDIAN · ▼ BELOW · ", f"{round(preds['guardian']*100)}%")
    fig.text(0.06, 0.095, f"Season:  Oracle {season[0]}  |  Guardian {season[1]}",
             color=MUTED, fontsize=15, va="center")
    return _finish(fig, out, hero, chips)


def build_resolution_card(out, close_px, level, preds, winner, season, streak,
                          period_label, closes=None):
    """Template A — morning resolution. winner: 'oracle' | 'guardian' | None."""
    if closes is None:
        closes = load_closes(14)
    fig = plt.figure(figsize=(10.8, 10.8), dpi=100, facecolor=BG)
    _header(fig, big=True)
    fig.text(0.06, 0.905, period_label.upper(), color=MUTED, fontsize=16, va="center")
    hero = fig.text(0.055, 0.795, f"${close_px:,.0f}", color=TEXT, fontsize=158,
                    fontweight="bold", va="center", fontfamily=NUM)
    fig.text(0.062, 0.690, "DAILY CLOSE", color=MUTED, fontsize=20, va="center")
    _chart(fig, closes, level, marker_px=close_px)

    chips = []
    def spec(agent):
        arrow = "▲ ABOVE" if agent == "oracle" else "▼ BELOW"
        color = ORACLE if agent == "oracle" else GUARDIAN
        name = f"{agent.upper()} · {arrow} ${level:,.0f} · "
        pct = f"{round(preds[agent]*100)}%"
        return color, name, pct
    if winner in ("oracle", "guardian"):
        loser = "guardian" if winner == "oracle" else "oracle"
        order = [(winner, 0.54, dict(edge=WIN, scale=1.1)),
                 (loser, 0.32, dict(dim=True, scale=0.85))]
        # keep Oracle on the left regardless of who won
        order.sort(key=lambda t: 0 if t[0] == "oracle" else 1)
        x = 0.06
        for agent, w, kw in order:
            color, name, pct = spec(agent)
            if agent == winner:
                pct += " ✓"
            _chip(fig, chips, x, w, color, name, pct, **kw)
            x += w + 0.02
    else:  # both missed (close exactly on the level) — show both dimmed
        for agent, x in (("oracle", 0.06), ("guardian", 0.52)):
            color, name, pct = spec(agent)
            _chip(fig, chips, x, 0.42, color, name, pct, dim=True)

    foot = f"Season:  Oracle {season[0]}  |  Guardian {season[1]}"
    if streak:
        foot += f"       Streak:  {streak}"
    fig.text(0.06, 0.095, foot, color=MUTED, fontsize=15, va="center")
    return _finish(fig, out, hero, chips)
