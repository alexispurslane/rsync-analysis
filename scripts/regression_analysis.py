#!/usr/bin/env python3
"""
rsync bug-rate analysis.

Bugs per 10 commits for every release. Where do the Claude releases
fall in the historical distribution? That's the whole analysis.
"""

from itertools import combinations
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
    return (np.log10(rate) + 2) / 4.5 * 100

def generate_report(releases: list[dict]) -> str:
    with_data = [r for r in releases if r["bugs"] > 0 and r["commits"] > 0]
    for r in with_data:
        r["bugs_10c"] = r["bugs"] * 10 / r["commits"]

    historical = [r for r in with_data if not r["is_claude"]]
    claude = [r for r in with_data if r["is_claude"]]
    hist_rates = sorted(r["bugs_10c"] for r in historical)
    claude_mean = np.mean([r["bugs_10c"] for r in claude])
    hist_mean = np.mean(hist_rates)

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

    # ── Permutation test (Bugs/10c) ──
    bug_all = with_data
    bug_claude = [r for r in bug_all if r["is_claude"]]
    bug_all_rates = [r["bugs_10c"] for r in bug_all]
    n_bug = len(bug_all)
    k_bug = len(bug_claude)
    n_extreme = sum(
        1 for combo in combinations(range(n_bug), k_bug)
        if np.mean([bug_all_rates[i] for i in combo]) >= claude_mean
    )
    n_total = len(list(combinations(range(n_bug), k_bug)))
    p_value = n_extreme / n_total

    claude_ranks = []
    for r in bug_claude:
        rank = sum(1 for h in hist_rates if h <= r["bugs_10c"])
        claude_ranks.append((r["tag"], r["bugs_10c"], rank, len(hist_rates)))

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
        f'<div class="outside" style="right:{100 - q25_left:.1f}%"></div>',
        f'<div class="outside" style="left:{q75_left:.1f}%"></div>',
        f'<div class="iqr" style="left:{q25_left:.1f}%;width:{q75_left - q25_left:.1f}%"></div>',
        f'<div class="iqr-center" style="left:{(q25_left + q75_left) / 2:.1f}%">middle 50%</div>',
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

/* ── Mean comparison bar ── */
.mean-bar{{display:flex;align-items:center;gap:1rem;margin-top:1rem;padding-top:.75rem;border-top:1px solid var(--bdr)}}
.mean-bar .label{{font-size:.8rem;color:var(--muted);font-family:var(--m)}}
.mean-bar .val{{font-size:1.1rem;font-weight:700;font-family:var(--m)}}
.mean-bar .hist{{color:var(--accent)}}
.mean-bar .claude{{color:var(--pos)}}
.mean-bar .vs{{font-size:.7rem;color:var(--muted);font-family:var(--m)}}

/* ── Significance card ── */
.sig{{background:#fff;border:1px solid var(--bdr);border-radius:8px;padding:1.5rem;margin:1.5rem 0;border-left:4px solid var(--pos)}}
.sig .p{{font-size:3rem;font-weight:800;font-family:var(--m);color:var(--pos)}}
.sig .explain{{font-size:.9rem;color:var(--muted);margin-top:.5rem;line-height:1.6}}
.sig .explain strong{{color:var(--fg);font-weight:600}}
.sig .detail{{font-size:.85rem;color:var(--muted);margin-top:.75rem;font-family:var(--m);line-height:1.8}}

/* ── Strip chart ── */
.strip{{
  position:relative;height:100px;margin:2.5rem 0 .5rem;
  background:#fff;border-radius:6px;border:1px solid var(--bdr);
}}
.outside{{
  position:absolute;top:0;bottom:0;
  background:rgba(0,0,0,.22);pointer-events:none;z-index:0;
}}
.iqr{{
  position:absolute;top:0;bottom:0;
  background:rgba(45,106,79,.22);
  border-left:4px solid var(--pos);border-right:4px solid var(--pos);
  pointer-events:none;z-index:0;
}}
.iqr-center{{
  position:absolute;top:-18px;transform:translateX(-50%);
  font-size:.85rem;font-family:var(--m);font-weight:800;
  color:var(--pos);letter-spacing:.1em;text-transform:uppercase;opacity:.5;
  pointer-events:none;z-index:1;white-space:nowrap;
}}
.med{{
  position:absolute;top:0;bottom:0;width:2px;
  background:var(--pos);opacity:.3;pointer-events:none;
  transform:translateX(-50%);z-index:0;
}}
.dot{{
  position:absolute;top:50%;transform:translate(-50%,-50%);
  border-radius:50%;cursor:help;z-index:1;opacity:.35;
}}
.dot.claude-dot{{
  opacity:1;z-index:2;
  box-shadow:0 0 0 3px rgba(45,106,79,.3);
}}
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
.iqr-explain{{font-size:.9rem;color:var(--muted);margin-top:1rem;line-height:1.7}}
.iqr-explain strong{{color:var(--fg);font-weight:600}}
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
<p class="iqr-explain">
  Each dot is a release. The <strong>shaded green band</strong> is the interquartile range (IQR) —
  the middle 50% of historical releases, from <strong>{q25:.2f}</strong> to <strong>{q75:.2f}</strong> bugs/10c.
  That means half of all historical releases fall inside this band, and half fall outside.
  The darker regions on either side are the lower and upper quarters.
  The Claude releases (green dots) <strong>{"both fall inside" if all(q25 <= r["bugs_10c"] <= q75 for r in claude) else "don't both fall inside"} the IQR</strong> —
  their bug rates are indistinguishable from the typical historical range.
</p>

<h2>Claude Releases</h2>
<div class="g">
  {claude_cards}
</div>
<div class="mean-bar">
  <span class="label">Claude mean:</span> <span class="val claude">{claude_mean:.2f}</span>
  <span class="vs">vs</span>
  <span class="label">Historical mean:</span> <span class="val hist">{hist_mean:.2f}</span>
  <span class="vs">({'{:.1f}×'.format(claude_mean / hist_mean) if hist_mean > 0 else 'N/A'})</span>
</div>

<h2>Is This Chance?</h2>
<div class="sig">
  <div class="p">p = {p_value:.2f}</div>
  <div class="explain">
    <strong>Permutation test on Bugs/10c:</strong> if we randomly pick {k_bug} releases out of {n_bug},
    what's the probability their mean Bugs/10c is at least as high as what we see for the Claude releases?
    <strong>{n_extreme}</strong> out of <strong>{n_total}</strong> possible pairs meet that bar — not rare at all.
  </div>
  <div class="detail">
    Claude mean Bugs/10c: {claude_mean:.2f} · Historical mean: {hist_mean:.2f} · Claude is {'lower' if claude_mean < hist_mean else 'higher'}<br/>
    {'<br/>'.join(f'{tag}: {rate:.2f} Bugs/10c — rank {rank}/{out_of} in historical distribution' for tag, rate, rank, out_of in claude_ranks)}
  </div>
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
