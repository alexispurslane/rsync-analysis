#!/usr/bin/env python3
"""
rsync bug-rate analysis.

Bugs per 10 commits for every release. Where do the Claude releases
fall in the historical distribution? That's the whole analysis.
"""

from pathlib import Path

import duckdb
import numpy as np

DB_PATH = Path(__file__).resolve().parent.parent.parent / "rsync_github.duckdb"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs"

def load_data(con: duckdb.DuckDBPyConnection) -> list[dict]:
    rows = con.execute("""
        SELECT tag_name, bug_count, total_commits, wt_sec, claude_commits
        FROM release_table
        WHERE tag_name NOT LIKE 'mbp%'
        ORDER BY tag_name
    """).fetchall()
    return [dict(zip(
        ["tag", "bugs", "commits", "wt_sec", "claude", "is_claude"],
        [r[0], r[1], r[2], float(r[3]), r[4], r[4] > 0]
    )) for r in rows]

def log_pct(rate: float) -> float:
    """Map bugs/10c to % position on a log scale (0.01→300 = 0→100%)."""
    if rate <= 0:
        return 0
    # 4.5 decades: log10(0.01)=-2 maps to 0%, log10(~316)=2.5 maps to 100%
    return (np.log10(rate) + 2) / 4.5 * 100

def generate_report(releases: list[dict]) -> str:
    with_data = [r for r in releases if r["bugs"] > 0 and r["commits"] > 0]
    for r in with_data:
        r["bugs_10c"] = r["bugs"] * 10 / r["commits"]

    historical = [r for r in with_data if not r["is_claude"]]
    claude = [r for r in with_data if r["is_claude"]]
    hist_rates = sorted(r["bugs_10c"] for r in historical)

    for r in claude:
        r["percentile"] = np.searchsorted(hist_rates, r["bugs_10c"]) / len(hist_rates) * 100
        r["rank"] = sum(1 for h in hist_rates if h <= r["bugs_10c"])
        r["out_of"] = len(hist_rates)

    sorted_data = sorted(with_data, key=lambda x: x["tag"])

    # ── IQR ──
    q25 = np.percentile(hist_rates, 25)
    q75 = np.percentile(hist_rates, 75)
    median = np.median(hist_rates)
    q25_left = log_pct(q25)
    q75_left = log_pct(q75)

    # ── Time-series (WtSec/10c over releases) ──
    # Only include releases with positive wt_sec so log scale works
    ts_releases = [r for r in releases if r["commits"] > 0 and r["wt_sec"] > 0]
    ts_max = max(r["wt_sec"] / r["commits"] * 10 for r in ts_releases)

    # Log scale spanning from 0.01 to just past ts_max
    ts_log_floor = -2  # log10(0.01)
    ts_log_ceil = np.ceil(np.log10(ts_max * 2))
    ts_decades = ts_log_ceil - ts_log_floor

    # SVG coordinate system
    svg_w, svg_h = 900, 300
    margin_l, chart_top, chart_bot = 60, 20, 250
    chart_w = svg_w - margin_l - 20  # right padding
    chart_h = chart_bot - chart_top

    def ts_y(rate: float) -> float:
        "Map rate to SVG y coordinate."
        log_val = np.log10(rate) if rate > 0 else ts_log_floor
        frac = (log_val - ts_log_floor) / ts_decades
        return chart_bot - frac * chart_h

    # Y-axis gridlines
    ts_ticks = []
    nice_vals = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50]
    for val in nice_vals:
        if ts_log_floor <= np.log10(val) <= ts_log_ceil:
            y = ts_y(val)
            ts_ticks.append(
                f'<line x1="{margin_l}" y1="{y:.1f}" x2="{margin_l + chart_w}" y2="{y:.1f}" class="ts-grid"/>'
                f'<text x="{margin_l - 5}" y="{y + 4:.1f}" class="ts-y-label">{val}</text>'
            )

    n = len(ts_releases)
    bar_w = chart_w / n * 0.75
    ts_bars = []

    for i, r in enumerate(ts_releases):
        rate = r["wt_sec"] / r["commits"] * 10
        is_c = r["is_claude"]
        cx = margin_l + (i + 0.5) / n * chart_w  # center of bar
        x = cx - bar_w / 2
        y_top = ts_y(rate)
        h = chart_bot - y_top
        if h < 1 and not is_c:
            continue
        color = "#2d6a4f" if is_c else "#b44a1e"
        opacity = "1" if is_c else "0.5"
        ts_bars.append(
            f'<rect x="{x:.1f}" y="{y_top:.1f}" width="{bar_w:.1f}" height="{max(h, 1):.1f}" '
            f'fill="{color}" opacity="{opacity}" rx="1" class="ts-bar">'
            f'<title>{r["tag"]}: {rate:.2f} WtSec/10c</title>'
            f'</rect>\n'
        )
        # Label Claude bars
        if is_c:
            ts_bars.append(
                f'<text x="{cx:.1f}" y="{y_top - 4:.1f}" class="ts-label">{r["tag"]}</text>\n'
            )
        # X-axis labels for select major versions
        tag = r["tag"]
        if tag in ("v1.6.4", "v2.0.0", "v2.3.0", "v2.5.0", "v2.6.0", "v3.0.0", "v3.1.0", "v3.2.0", "v3.3.0", "v3.4.0", "v3.4.2", "v3.4.3"):
            ts_bars.append(
                f'<text x="{cx:.1f}" y="{chart_bot + 14:.1f}" class="ts-x-label" '
                f'transform="rotate(-40 {cx:.1f} {chart_bot + 14:.1f})">{tag}</text>\n'
            )

    ts_svg = ''.join(ts_ticks) + ''.join(ts_bars)

    # ── Table ──
    table_rows = ""
    for r in sorted_data:
        is_c = r["is_claude"]
        pctile = f'{r["percentile"]:.0f}th' if is_c else ""
        table_rows += (
            f'<tr class="{"claude-era" if is_c else ""}">'
            f'<td class="rel">{r["tag"]}</td>'
            f'<td class="n">{r["bugs"]}</td>'
            f'<td class="n">{r["commits"]}</td>'
            f'<td class="n">{r["wt_sec"]:.1f}</td>'
            f'<td class="n">{r["claude"]}</td>'
            f'<td class="n rate">{r["bugs_10c"]:.2f}</td>'
            f'<td class="era">{pctile}</td></tr>\n          '
        )

    # ── Strip chart ──
    strip_parts = [
        # Grey out outside-IQR regions
        f'<div class="outside" style="right:{100 - q25_left:.1f}%"></div>',
        f'<div class="outside" style="left:{q75_left:.1f}%"></div>',
        # IQR green band
        f'<div class="iqr" style="left:{q25_left:.1f}%;width:{q75_left - q25_left:.1f}%"></div>',
        # Centered "middle 50%" watermark
        f'<div class="iqr-center" style="left:{(q25_left + q75_left) / 2:.1f}%">middle 50%</div>',
        # Median line
        f'<div class="med" style="left:{log_pct(median):.1f}%"></div>',
    ]

    for r in sorted_data:
        is_c = r["is_claude"]
        color = "#2d6a4f" if is_c else "#b44a1e"
        size = 18 if is_c else 11
        left = log_pct(r["bugs_10c"])
        in_iqr = q25 <= r["bugs_10c"] <= q75
        dot_class = "dot claude-dot" if is_c else "dot"
        strip_parts.append(
            f'<div class="{dot_class}" '
            f'style="left:{left:.1f}%;background:{color};width:{size}px;height:{size}px" '
            f'title="{r["tag"]}: {r["bugs_10c"]:.2f} bugs/10c"></div>'
        )
        # Label Claude dots
        if is_c:
            strip_parts.append(
                f'<div class="dot-tag" style="left:{left:.1f}%">'
                f'{r["tag"]}'
                f'<span class="badge">inside middle 50% ✓</span></div>'
            )

    strip_items = "\n  ".join(strip_parts)

    # ── Claude cards ──
    claude_cards = ""
    for r in claude:
        in_iqr = q25 <= r["bugs_10c"] <= q75
        claude_cards += (
            f'<div class="c">'
            f'<h3>{r["tag"]}</h3>'
            f'<div class="b">{r["bugs_10c"]:.2f} <span class="u">bugs/10c</span></div>'
            f'<div class="d">{r["bugs"]} bugs · {r["commits"]} commits · {r["claude"]} Claude</div>'
            f'<div class="d pctile">{r["percentile"]:.0f}th percentile (rank {r["rank"]} of {r["out_of"]})</div>'
            f'<div class="d iqr-tag">{"Inside" if in_iqr else "Outside"} the middle 50% ({q25:.2f}–{q75:.2f})</div>'
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bugs/10c Distribution — rsync</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root{{--bg:#faf6f0;--fg:#3c2a1a;--muted:#7a6b5d;--accent:#b44a1e;--pos:#2d6a4f;--bdr:#d9cfc4;--s:'Inter',sans-serif;--m:'JetBrains Mono',monospace}}
*{{box-sizing:border-box;margin:0;padding:0}}html{{font-size:17px}}
body{{font-family:var(--s);color:var(--fg);background:var(--bg);line-height:1.7;max-width:920px;margin:0 auto;padding:2rem 1.5rem}}
h1{{font-size:1.8rem;font-weight:700;margin-bottom:.5rem}}
h2{{font-size:1.3rem;font-weight:600;margin:2.5rem 0 .75rem;color:var(--accent)}}
p{{margin-bottom:.75rem}}.sub{{color:var(--muted);font-size:.95rem;margin-bottom:2rem}}
table{{width:100%;border-collapse:collapse;font-size:.85rem;margin:1rem 0}}
th{{text-align:left;font-weight:600;padding:.4rem .6rem;border-bottom:2px solid var(--bdr);color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.03em}}
td{{padding:.35rem .6rem;border-bottom:1px solid var(--bdr)}}
td.n{{text-align:right;font-family:var(--m);font-size:.82rem}}
td.rel{{font-family:var(--m);font-weight:500}}
td.rate{{font-weight:600}}
tr.claude-era{{background:rgba(45,106,79,.08)}}
td.era{{color:var(--pos);font-weight:600;font-size:.82rem;font-family:var(--m)}}
.g{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:1.5rem 0}}
.c{{background:#fff;border:1px solid var(--bdr);border-radius:8px;padding:1.25rem}}
.c h3{{font-size:.95rem;color:var(--fg);margin:0 0 .5rem 0;font-weight:600}}
.c .b{{font-size:2.2rem;font-weight:700;font-family:var(--m)}}
.c .u{{font-size:.85rem;color:var(--muted);font-weight:400}}
.c .d{{font-size:.85rem;color:var(--muted);margin-top:.25rem}}
.c .pctile{{color:var(--pos);font-weight:600;margin-top:.5rem;font-size:.95rem}}
.c .iqr-tag{{color:var(--pos);font-weight:700;margin-top:.5rem;font-size:.95rem}}

/* ── Strip chart ── */
.strip{{
  position:relative;height:100px;margin:2.5rem 0 .5rem;
  background:#fff;border-radius:6px;border:1px solid var(--bdr);
}}
/* Greyed-out outside-IQR regions */
.outside{{
  position:absolute;top:0;bottom:0;
  background:rgba(0,0,0,.22);pointer-events:none;z-index:0;
}}
/* IQR green band */
.iqr{{
  position:absolute;top:0;bottom:0;
  background:rgba(45,106,79,.22);
  border-left:4px solid var(--pos);border-right:4px solid var(--pos);
  pointer-events:none;z-index:0;
}}
/* "middle 50%" centered label inside the band */
.iqr-center{{
  position:absolute;top:-18px;transform:translateX(-50%);
  font-size:.85rem;font-family:var(--m);font-weight:800;
  color:var(--pos);letter-spacing:.1em;text-transform:uppercase;opacity:.5;
  pointer-events:none;z-index:1;white-space:nowrap;
}}
/* Q1/Q3 boundary labels at the edges */
.q-label{{
  position:absolute;bottom:4px;transform:translateX(-50%);
  pointer-events:none;z-index:4;text-align:center;
}}
.q-val{{
  display:inline-block;font-size:.7rem;color:var(--pos);
  font-family:var(--m);font-weight:800;
  background:var(--bg);padding:2px 6px;
  border:2px solid var(--pos);border-radius:4px;
}}
/* Median line */
.med{{
  position:absolute;top:0;bottom:0;width:2px;
  background:var(--pos);opacity:.3;pointer-events:none;
  transform:translateX(-50%);z-index:0;
}}
/* Dots */
.dot{{
  position:absolute;top:50%;transform:translate(-50%,-50%);
  border-radius:50%;cursor:help;z-index:1;opacity:.35;
}}
.dot.claude-dot{{
  opacity:1;z-index:2;
  box-shadow:0 0 0 3px rgba(45,106,79,.3);
}}
/* Claude dot labels */
.dot-tag{{
  position:absolute;top:calc(50% + 18px);transform:translateX(-50%);
  font-size:.7rem;font-family:var(--m);font-weight:800;
  color:var(--pos);text-align:center;line-height:1.3;
  pointer-events:none;white-space:nowrap;z-index:3;
}}
.dot-tag .badge{{
  display:inline-block;font-size:.55rem;font-weight:700;
  color:#fff;background:var(--pos);
  padding:1px 5px;border-radius:3px;margin-left:3px;
  vertical-align:middle;
}}
.axis{{display:flex;justify-content:space-between;font-size:.7rem;color:var(--muted);font-family:var(--m);margin-top:4px}}
.legend{{display:flex;gap:1.5rem;margin-top:.75rem;font-size:.85rem;color:var(--muted);flex-wrap:wrap}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:.3rem;vertical-align:middle}}

/* ── Time-series chart ── */
.ts-chart{{width:100%;height:340px;margin:2rem 0 .5rem;background:#fff;border-radius:6px;border:1px solid var(--bdr);overflow:visible}}
.ts-bar{{cursor:help}}
.ts-label{{font-size:.65rem;font-family:var(--m);font-weight:700;fill:var(--pos);text-anchor:middle}}
.ts-y-label{{font-size:.6rem;fill:var(--muted);font-family:var(--m);dominant-baseline:hanging}}
.ts-x-label{{font-size:.55rem;fill:var(--muted);font-family:var(--m);text-anchor:end}}
.ts-grid{{stroke:var(--bdr);stroke-width:.5;stroke-dasharray:4 3}}
</style></head><body>

<h1>Bugs per 10 Commits</h1>
<p class="sub">rsync releases with bug data · Where do the Claude releases sit in the distribution?</p>

<h2>The Distribution (log scale)</h2>
<div class="strip">
  {strip_items}
</div>
<div class="axis">
  <span>0.01</span><span>0.1</span><span>1</span><span>10</span><span>100</span>
</div>
<div class="legend">
  <span><i style="background:#b44a1e;opacity:.5"></i> Historical</span>
  <span><i style="background:#2d6a4f"></i> Claude</span>
  <span style="display:inline-flex;align-items:center;gap:4px">
    <span style="display:inline-block;width:24px;height:12px;background:rgba(45,106,79,.1);border-left:3px solid #2d6a4f;border-right:3px solid #2d6a4f;vertical-align:middle"></span>
    Middle 50% (IQR)
  </span>
  <span style="display:inline-flex;align-items:center;gap:4px">
    <span style="display:inline-block;width:24px;height:12px;background:rgba(0,0,0,.1);vertical-align:middle"></span>
    Outside IQR
  </span>
</div>

<h2>Claude Releases</h2>
<div class="g">
  {claude_cards}
</div>

<h2>WtSec/10c Over Time</h2>
<svg class="ts-chart" viewBox="0 0 900 300" preserveAspectRatio="xMidYMid meet">
  {ts_svg}
</svg>
<div class="legend">
  <span><i style="background:#b44a1e;opacity:.5"></i> Historical</span>
  <span><i style="background:#2d6a4f"></i> Claude</span>
</div>

<h2>All Releases (chronological)</h2>
<table>
  <tr><th>Release</th><th>Bugs</th><th>Commits</th><th>WtSec</th><th>Claude</th><th>Bugs/10c</th><th>Percentile</th></tr>
  {table_rows}
</table>

</body></html>"""
    return html

def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    releases = load_data(con)
    con.close()

    html = generate_report(releases)
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "index.html").write_text(html)
    print(f"Written to {OUTPUT_DIR / 'index.html'}")

if __name__ == "__main__":
    main()
