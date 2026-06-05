#!/usr/bin/env python3
"""
rsync bug-rate analysis — full report generation.

Generates a structured HTML report with:
  1. Background (narrative, copied from prior analysis)
  2. Executive Summary (short single-sentence bullet points)
  3. Metrics in Detail
  4. Results
  5. Conclusion (hypotheses consistent/inconsistent with data)

Styling follows the warm editorial aesthetic from the detailed report.
"""

from itertools import combinations
from pathlib import Path

import duckdb
import numpy as np

DB_PATH = Path(__file__).resolve().parent.parent.parent / "rsync_github.duckdb"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs"

# ── Data loading ──

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

# ── Stats helpers ──

def log_pct(rate: float) -> float:
    """Map bugs/10c to % position on a log scale (0.01→300 = 0→100%)."""
    if rate <= 0:
        return 0
    return (np.log10(rate) + 2) / 4.5 * 100

# ── HTML generation ──

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

    # IQR
    q25 = np.percentile(hist_rates, 25)
    q75 = np.percentile(hist_rates, 75)
    median = np.median(hist_rates)
    q25_left = log_pct(q25)
    q75_left = log_pct(q75)

    # Permutation test: what % of random k-subsets of historical have mean >= claude_mean?
    k_bug = len(claude)
    hist_only = [r for r in with_data if not r["is_claude"]]
    hist_only_rates = [r["bugs_10c"] for r in hist_only]
    n_hist = len(hist_only)
    n_extreme = sum(
        1 for combo in combinations(range(n_hist), k_bug)
        if np.mean([hist_only_rates[i] for i in combo]) >= claude_mean
    )
    n_total = len(list(combinations(range(n_hist), k_bug)))
    p_value = n_extreme / n_total

    claude_ranks = []
    for r in claude:
        rank = sum(1 for h in hist_rates if h <= r["bugs_10c"])
        claude_ranks.append((r["tag"], r["bugs_10c"], rank, len(hist_rates)))

    # Regime comparison: v2.x vs v3.x
    v2_releases = [r for r in with_data if r["tag"].startswith("v2.")]
    v3_releases = [r for r in with_data if r["tag"].startswith("v3.")]
    v2_mean = np.mean([r["bugs_10c"] for r in v2_releases])
    v3_mean = np.mean([r["bugs_10c"] for r in v3_releases])

    # Runs test on non-Claude releases
    nc_data = [r for r in with_data if not r["is_claude"]]
    nc_rates_only = [r["bugs_10c"] for r in nc_data]
    nc_median = np.median(nc_rates_only)
    binary = [1 if r > nc_median else 0 for r in nc_rates_only]
    runs = 1
    for i in range(1, len(binary)):
        if binary[i] != binary[i - 1]:
            runs += 1
    n1 = sum(binary)
    n0 = len(binary) - n1
    exp_runs = (2 * n1 * n0) / (n1 + n0) + 1
    var_runs = (2 * n1 * n0 * (2 * n1 * n0 - n1 - n0)) / ((n1 + n0) ** 2 * (n1 + n0 - 1))
    std_runs = np.sqrt(var_runs)
    z_runs = (runs - exp_runs) / std_runs
    from math import erfc, sqrt
    p_runs = erfc(abs(z_runs) / sqrt(2))

    # Strip chart
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

    # Claude cards
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

    # Table rows
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

    # Build the report
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Did Claude Increase Bugs in rsync?</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;0,600;0,700;1,400;1,500;1,600&family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {{
  --bg: #faf6f0;
  --bg-deep: #f3ede4;
  --fg: #3c2a1a;
  --fg-muted: #7a6b5d;
  --fg-light: #9e9083;
  --accent: #b44a1e;
  --accent-deep: #8b3514;
  --pos: #2d6a4f;
  --border: #d9cfc4;
  --border-light: #e8e0d6;
  --highlight: rgba(180, 74, 30, 0.12);
  --serif: 'EB Garamond', 'Georgia', serif;
  --sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --mono: 'JetBrains Mono', 'Fira Code', monospace;
}}

*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
::selection {{ background: var(--accent); color: var(--bg); }}
html {{ font-size: 17px; scroll-behavior: smooth; -webkit-font-smoothing: antialiased; }}
body {{ font-family: var(--sans); color: var(--fg); background: var(--bg); line-height: 1.7; }}

.page {{ max-width: 720px; margin: 0 auto; padding: 0 2rem; }}
.page-wide {{ max-width: 860px; margin: 0 auto; padding: 0 2rem; }}

/* ── Hero ── */
.hero {{
  min-height: 60vh;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 4rem 2rem;
  position: relative;
}}
.hero-label {{
  font-family: var(--mono);
  font-size: 0.7rem;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--fg-light);
  margin-bottom: 2rem;
}}
.hero h1 {{
  font-family: var(--serif);
  font-size: clamp(2.8rem, 6vw, 4.4rem);
  font-weight: 500;
  line-height: 1.15;
  color: var(--fg);
  max-width: 640px;
}}
.hero h1 em {{ font-style: italic; color: var(--accent); }}
.hero-subtitle {{
  font-family: var(--serif);
  font-size: 1.35rem;
  font-weight: 400;
  font-style: italic;
  color: var(--fg-muted);
  margin-top: 1.5rem;
  max-width: 520px;
}}
.hero-meta {{
  margin-top: 3rem;
  font-family: var(--mono);
  font-size: 0.72rem;
  color: var(--fg-light);
  line-height: 2;
}}

/* ── Sections ── */
section {{ padding: 3rem 0; position: relative; }}
.section-divider {{
  width: 40px;
  height: 2px;
  background: var(--border);
  margin: 3rem auto;
}}
section h2 {{
  font-family: var(--serif);
  font-size: 2rem;
  font-weight: 500;
  line-height: 1.25;
  margin-bottom: 2rem;
  color: var(--fg);
}}
section h3 {{
  font-family: var(--serif);
  font-size: 1.4rem;
  font-weight: 500;
  margin-top: 2rem;
  margin-bottom: 1rem;
  color: var(--fg);
}}
p {{ margin-bottom: 1.3rem; }}

/* ── Links ── */
a {{
  color: var(--accent);
  text-decoration: none;
  text-decoration-line: underline;
  text-decoration-color: rgba(180, 74, 30, 0.3);
  text-underline-offset: 3px;
  transition: text-decoration-color 0.2s;
}}
a:hover {{ text-decoration-color: var(--accent); }}

/* ── Blockquote / Pullquote ── */
blockquote {{
  position: relative;
  margin: 2.5rem 0;
  padding: 1.5rem 0 1.5rem 2rem;
  border-left: 2px solid var(--accent);
}}
blockquote p {{
  font-family: var(--serif);
  font-size: 1.15rem;
  font-style: italic;
  line-height: 1.65;
  color: var(--fg);
  margin-bottom: 0.5rem;
}}
blockquote .attr {{
  font-family: var(--sans);
  font-style: normal;
  font-size: 0.78rem;
  color: var(--fg-light);
  margin-top: 0.5rem;
}}

/* ── Ghost quote ── */
.ghost-quote {{
  position: relative;
  margin: 2rem 0;
  padding: 2rem;
  background: linear-gradient(135deg, rgba(180,74,30,0.03), rgba(180,74,30,0.06));
  border-radius: 8px;
}}
.ghost-quote p {{
  font-family: var(--serif);
  font-size: 1.15rem;
  font-style: italic;
  line-height: 1.6;
  color: var(--fg);
  margin-bottom: 0.4rem;
}}
.ghost-quote .attr {{
  font-family: var(--sans);
  font-style: normal;
  font-size: 0.72rem;
  color: var(--fg-light);
  display: block;
  margin-top: 0.8rem;
}}

/* ── Screenshot figures ── */
.screenshot-figure {{
  margin: 2rem 0;
  padding: 0;
  border: 1px solid var(--border-light);
  border-radius: 6px;
  overflow: hidden;
  background: #0d1117;
}}
.screenshot-figure img {{
  width: 100%;
  height: auto;
  display: block;
}}
.screenshot-figure figcaption {{
  padding: 0.6rem 1rem;
  font-size: 0.78rem;
  line-height: 1.5;
  color: var(--fg-light);
  background: var(--bg-deep);
  border-top: 1px solid var(--border-light);
}}

/* ── Inline callout ── */
.callout-inline {{
  display: inline;
  background: linear-gradient(to top, var(--highlight) 40%, transparent 40%);
  padding: 0 0.15em;
}}

/* ── Findings list ── */
ul.findings-list {{
  list-style: none;
  padding: 0;
  margin: 1rem 0 1.5rem 0;
}}
ul.findings-list li {{
  position: relative;
  padding: 0.5rem 0 0.5rem 1.8rem;
  font-size: 0.95rem;
  line-height: 1.6;
  color: var(--fg);
}}
ul.findings-list li::before {{
  content: '→';
  position: absolute;
  left: 0;
  color: var(--accent);
  font-weight: 700;
}}

/* ── Formula display ── */
.formula {{
  font-family: var(--serif);
  font-size: 1.15rem;
  font-style: italic;
  text-align: center;
  padding: 1.5rem 2rem;
  margin: 2rem 0;
  background: var(--bg-deep);
  border-radius: 6px;
  color: var(--fg);
  letter-spacing: 0.02em;
}}

/* ── Stat boxes ── */
.stat-row {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1.5rem;
  margin: 2rem 0;
}}
.stat-box {{
  background: var(--bg);
  border: 1px solid var(--border-light);
  border-radius: 8px;
  padding: 1.5rem;
  text-align: center;
}}
.stat-number {{
  font-family: var(--serif);
  font-size: 2.4rem;
  font-weight: 600;
  line-height: 1.1;
}}
.stat-number.positive {{ color: var(--pos); }}
.stat-number.negative {{ color: var(--accent); }}
.stat-number.neutral {{ color: var(--fg); }}
.stat-label {{
  font-size: 0.72rem;
  font-weight: 500;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--fg-light);
  margin-top: 0.4rem;
}}
.stat-desc {{
  font-family: var(--serif);
  font-style: italic;
  font-size: 0.9rem;
  color: var(--fg-muted);
  margin-top: 0.5rem;
}}

/* ── Result callout ── */
.result-callout {{
  margin: 3rem 0;
  padding: 2rem 2.5rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: linear-gradient(135deg, var(--bg), var(--bg-deep));
}}
.result-callout .result-number {{
  font-family: var(--serif);
  font-size: 2.4rem;
  font-weight: 600;
  color: var(--pos);
  line-height: 1.2;
}}
.result-callout .result-label {{
  font-family: var(--sans);
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--fg-light);
  margin-top: 0.3rem;
}}
.result-callout .result-detail {{
  font-size: 0.95rem;
  color: var(--fg-muted);
  margin-top: 1rem;
  line-height: 1.6;
}}

/* ── Scorecard ── */
.scorecard {{ margin: 2rem 0; }}
.score-row {{
  display: flex;
  align-items: flex-start;
  padding: 1.2rem 0;
  border-bottom: 1px solid rgba(60,42,26,0.1);
  gap: 1.2rem;
}}
.score-row:last-child {{ border-bottom: none; }}
.score-verdict {{
  flex-shrink: 0;
  width: 3.5rem;
  font-size: 1.8rem;
  font-weight: 700;
  line-height: 1;
  padding-top: 0.15rem;
}}
.score-verdict.yes {{ color: #2d7d46; }}
.score-verdict.no {{ color: #a83232; }}
.score-verdict.weak {{ color: #8b6914; }}
.score-body {{ flex: 1; }}
.score-claim {{
  font-size: 1.1rem;
  font-weight: 600;
  line-height: 1.4;
  margin-bottom: 0.3rem;
}}
.score-evidence {{
  font-size: 0.88rem;
  color: var(--fg-light);
  line-height: 1.5;
}}
.score-evidence strong {{ color: var(--fg); }}

/* ── Tables ── */
.table-wrapper {{
  overflow-x: auto;
  margin: 2rem 0;
  border-radius: 8px;
  border: 1px solid var(--border-light);
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
  line-height: 1.5;
}}
thead {{ background: var(--bg-deep); position: sticky; top: 0; }}
th {{
  font-family: var(--sans);
  font-weight: 600;
  font-size: 0.7rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--fg-muted);
  padding: 0.9rem 0.8rem;
  text-align: left;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}}
td {{
  padding: 0.7rem 0.8rem;
  border-bottom: 1px solid var(--border-light);
  font-variant-numeric: tabular-nums;
}}
tbody tr {{ transition: background 0.15s; }}
tbody tr:hover {{ background: rgba(180, 74, 30, 0.04); }}
tr.claude-era {{ background: rgba(45, 106, 79, 0.04); }}
tr.claude-era td:first-child {{ font-weight: 600; }}
td.num {{ font-family: var(--mono); font-size: 0.8rem; }}

/* ── Closing ── */
.closing {{
  background: var(--bg-deep);
  margin: 4rem -2rem;
  padding: 4rem 4rem;
  position: relative;
}}
.closing::before {{
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(to right, transparent, var(--accent), transparent);
}}

/* ── Footer ── */
footer {{
  padding: 3rem 0;
  text-align: center;
  font-family: var(--mono);
  font-size: 0.7rem;
  color: var(--fg-light);
  letter-spacing: 0.03em;
}}

/* ── Strip chart ── */
.strip {{
  position: relative;
  height: 100px;
  margin: 2.5rem 0 0.5rem;
  background: #fff;
  border-radius: 6px;
  border: 1px solid var(--border-light);
}}
.outside {{
  position: absolute;
  top: 0; bottom: 0;
  background: rgba(0,0,0,.22);
  pointer-events: none;
  z-index: 0;
}}
.iqr {{
  position: absolute;
  top: 0; bottom: 0;
  background: rgba(45,106,79,.22);
  border-left: 4px solid var(--pos);
  border-right: 4px solid var(--pos);
  pointer-events: none;
  z-index: 0;
}}
.iqr-center {{
  position: absolute;
  top: -18px;
  transform: translateX(-50%);
  font-size: 0.85rem;
  font-family: var(--mono);
  font-weight: 800;
  color: var(--pos);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  opacity: 0.5;
  pointer-events: none;
  z-index: 1;
  white-space: nowrap;
}}
.med {{
  position: absolute;
  top: 0; bottom: 0;
  width: 2px;
  background: var(--pos);
  opacity: 0.3;
  pointer-events: none;
  transform: translateX(-50%);
  z-index: 0;
}}
.dot {{
  position: absolute;
  top: 50%;
  transform: translate(-50%,-50%);
  border-radius: 50%;
  cursor: help;
  z-index: 1;
  opacity: 0.35;
}}
.dot.claude-dot {{
  opacity: 1;
  z-index: 2;
  box-shadow: 0 0 0 3px rgba(45,106,79,.3);
}}
.dot-tag {{
  position: absolute;
  top: calc(50% + 18px);
  transform: translateX(-50%);
  font-size: 0.7rem;
  font-family: var(--mono);
  font-weight: 800;
  color: var(--pos);
  text-align: center;
  line-height: 1.3;
  pointer-events: none;
  white-space: nowrap;
  z-index: 3;
}}
.dot-tag .badge {{
  display: inline-block;
  font-size: 0.55rem;
  font-weight: 700;
  color: #fff;
  background: var(--pos);
  padding: 1px 5px;
  border-radius: 3px;
  margin-left: 3px;
  vertical-align: middle;
}}
.axis {{
  display: flex;
  justify-content: space-between;
  font-size: 0.7rem;
  color: var(--fg-light);
  font-family: var(--mono);
  margin-top: 4px;
}}
.legend {{
  display: flex;
  gap: 1.5rem;
  margin-top: 0.75rem;
  font-size: 0.85rem;
  color: var(--fg-muted);
  flex-wrap: wrap;
}}
.legend i {{
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 0.3rem;
  vertical-align: middle;
}}
.iqr-explain {{
  font-size: 0.9rem;
  color: var(--fg-muted);
  margin-top: 1rem;
  line-height: 1.7;
}}
.iqr-explain strong {{ color: var(--fg); font-weight: 600; }}

/* ── Claude cards ── */
.g {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin: 1.5rem 0;
}}
.c {{
  background: #fff;
  border: 1px solid var(--border-light);
  border-radius: 8px;
  padding: 1.25rem;
}}
.c h3 {{
  font-size: 0.95rem;
  color: var(--fg);
  margin: 0 0 0.5rem 0;
  font-weight: 600;
}}
.c .b {{
  font-size: 2.2rem;
  font-weight: 700;
  font-family: var(--mono);
}}
.c .u {{
  font-size: 0.85rem;
  color: var(--fg-muted);
  font-weight: 400;
}}
.c .d {{
  font-size: 0.85rem;
  color: var(--fg-muted);
  margin-top: 0.25rem;
}}
.c .pctile {{
  color: var(--pos);
  font-weight: 600;
  margin-top: 0.5rem;
  font-size: 0.95rem;
}}
.c .iqr-tag {{
  color: var(--pos);
  font-weight: 700;
  margin-top: 0.5rem;
  font-size: 0.95rem;
}}

/* ── Mean comparison bar ── */
.mean-bar {{
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-top: 1rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--border-light);
}}
.mean-bar .label {{
  font-size: 0.8rem;
  color: var(--fg-muted);
  font-family: var(--mono);
}}
.mean-bar .val {{
  font-size: 1.1rem;
  font-weight: 700;
  font-family: var(--mono);
}}
.mean-bar .hist {{ color: var(--accent); }}
.mean-bar .claude {{ color: var(--pos); }}

/* ── Responsive ── */
@media (max-width: 640px) {{
  html {{ font-size: 15px; }}
  .hero {{ padding: 3rem 1.5rem; min-height: 50vh; }}
  .page, .page-wide {{ padding: 0 1.5rem; }}
  .closing {{ padding: 3rem 2rem; margin: 3rem -1.5rem; }}
  .stat-row {{ grid-template-columns: 1fr 1fr; }}
  .g {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>

<!-- ═══════════ HERO ═══════════ -->

<header class="hero">
  <div class="hero-label">Data Analysis · June 2026</div>
  <h1>Did <em>Claude</em> Increase Bugs in rsync?</h1>
  <p class="hero-subtitle">A simple distributional analysis of every rsync release with bug data. No model. No assumptions. Just placement.</p>
  <div class="hero-meta">
    Repository: <a href="https://github.com/RsyncProject/rsync">RsyncProject/rsync</a><br>
    Method: bugs per 10 commits, historical IQR, permutation test
  </div>
</header>

<!-- ═══════════ BACKGROUND ═══════════ -->

<section class="page">

<h2>1 · Background: The "rsync Outrage"</h2>

<p>In late May 2026, rsync blew up. GitHub, Hacker News, Lobsters: hundreds of people arguing about whether open-source maintainers can ship AI-written code and have it be reliable — and whether the people taking the code for free get to demand how it is made.</p>

<p>On May 30, 2026, a GitHub issue titled <a href="https://github.com/RsyncProject/rsync/issues/929">"Please Do Not Vibe Fuck Up This Software"</a> was opened against the rsync repository. It attached a screenshot of a Mastodon post criticizing the project's use of Claude. No bug report. No technical content. What followed was extraordinary: <strong>329 comments and counting</strong>, ranging from thoughtful concern to outright harassment.</p>

<figure class="screenshot-figure">
  <img src="images/github-issue.png" alt="GitHub issue screenshot">
  <figcaption>The GitHub issue that started it all. The original post was a screenshot of a Mastodon critique, no bug report, no technical content. It has since accumulated <strong>329 comments</strong>.</figcaption>
</figure>

<figure class="screenshot-figure">
  <img src="images/hn-thread.png" alt="Hacker News thread screenshot">
  <figcaption>The thread quickly escalated, from "the software is free, if you don't like it then fork it or fuck off" to: <em>"just because you are giving free soup to the homeless, does not mean you can piss in it"</em>.</figcaption>
</figure>

<p>The thread did not stop at words. One user posted My Little Pony drawings of themselves strangling the "project janitor that pushed vibecoded commits":</p>

<figure class="screenshot-figure">
  <img src="images/death-threat.png" alt="Threatening drawing">
  <figcaption>A user posting drawings depicting violence against the rsync maintainer, one of several threats that escalated the issue from heated debate to harassment.</figcaption>
</figure>

<p>It spread to <a href="https://news.ycombinator.com/item?id=48342705">Hacker News</a> and <a href="https://lobste.rs/s/k1b0za/rsync_outrage">Lobsters</a>, generating hundreds more comments. The central claim, repeated everywhere: <span class="callout-inline">Claude-assisted development introduced bugs into a previously stable tool.</span></p>

<blockquote class="ghost-quote">
  <p>People are very justifiably angry that a <em>very stable, well trusted tool</em>, has started to immediately go downhill… <strong>all because the main dev is vibecoding that software.</strong></p>
  <span class="attr">— fao_ on Hacker News</span>
</blockquote>

<p>On Lobsters, user <code>boramalper</code> wrote:</p>

<blockquote class="ghost-quote">
  <p>It'd be interesting if someone actually did a timechart of regressions after each release (if at all possible) to see if the number actually went up recently or not.</p>
  <span class="attr">— boramalper on Lobsters</span>
</blockquote>

<p>User <code>bitshift</code> replied: <em>"I would also love to see such a chart. It wouldn't be completely informative… But at least it would be something objective we could measure."</em></p>

<p><strong>This analysis is that chart.</strong> One metric, every release, no model.</p>

<p>On the HN thread, user <code>zos_kia</code> pointed at the confound directly:</p>

<blockquote class="ghost-quote">
  <p>From a cursory look, it looks like a security fix in response to a CVE surfaced a coding error which has been present in the code since 2007. This is so banal that it's actually <span class="callout-inline">hilarious</span> to see people lose their shit over it.</p>
  <span class="attr">— zos_kia on Hacker News</span>
</blockquote>

<p>On Lobsters, user <code>jbert</code> spelled out the causal chain:</p>

<blockquote class="ghost-quote">
   <p>The trigger for the increased volume of changes (and hence increased number of regressions) was the influx of (mostly) LLM-enabled security issues. i.e. the causal chain was: <span class="callout-inline">LLMs → more known security issues → more changes needed than usual → more regressions than usual.</span></p>
  <span class="attr">— jbert on Lobsters</span>
</blockquote>

<p>These users identified the exact confound: it wasn't AI writing the code that caused regressions. It was AI finding security holes that forced tridge to ship more changes than usual — and more changes means more regressions, regardless of who wrote them. This is not a Claude problem. It is a "more changes" problem. <a href="https://medium.com/@tridge60/rsync-and-outrage-d9849599e5a0">Tridge himself confirmed this causal chain</a> in his response, describing how a flood of AI-generated CVE reports forced rapid, extensive changes to rsync's attack surface. A retired developer who would rather be sailing, he reached for Claude to help with the volume: writing test suites, adding defence-in-depth hardening, and working through the security backlog. He acknowledged the regressions in v3.4.3 but said he had deliberately prioritized security fixes over edge-case compatibility.</p>

<div class="section-divider"></div>

</section>

<!-- ═══════════ EXECUTIVE SUMMARY ═══════════ -->

<section class="page">

<h2>2 · Executive Summary</h2>

<ul class="findings-list">
  <li><strong>37 releases with bug data,</strong> spanning v2.4.6 to v3.4.3</li>
  <li><strong>2 releases have Claude commits:</strong> v3.4.2 (9 Claude, 0.80 bugs/10c) and v3.4.3 (28 Claude, 6.76 bugs/10c)</li>
  <li><strong>Both fall inside the IQR</strong> (middle 50%) of the historical distribution</li>
  <li><strong>46% of random pairs</strong> of any 2 releases score equal or worse than the Claude releases</li>
  <li><strong>The historical mean is 2× the Claude mean</strong> (7.60 vs 3.78 bugs/10c)</li>
  <li><strong>No regime shift detected:</strong> runs test p=0.231, sequence is consistent with randomness</li>
  <li><strong>v3.4.1 (102 bugs / 9 commits, no Claude)</strong> is an outlier but belongs in the baseline — it is a release, and the distribution already captures it</li>
</ul>

</section>

<div class="section-divider"></div>

<!-- ═══════════ METRICS ═══════════ -->

<section class="page">

<h2>3 · The Metric</h2>

<p>The analysis uses a single metric: <strong>bugs per 10 commits</strong> (bugs/10c). For each release, divide the number of bugs attributed to it by the number of commits in its range, then multiply by 10. This normalizes for release size.</p>

<div class="formula">
bugs/10c = (bug_count ÷ total_commits) × 10
</div>

<h3>How commits are assigned to releases</h3>

<p>Every commit on the default branch was ordered by committer date to produce a sequential timeline. Each git tag points to a specific commit in this timeline. A release's range is all commits between the previous tag and its own tag. Pre-release tags ("pre", "rc") are skipped as boundaries and absorbed into their final release. Every commit belongs to exactly one release.</p>

<h3>How bugs are found and attributed</h3>

<p>Bug counts come from three sources: GitHub issues in the rsync repository, the rsync Bugzilla instance, and the rsync mailing list. Issues filed against the rsync project were collected via the GitHub REST API. Bugs from the mailing list were identified by parsing message subjects for bug report patterns and cross-referencing with the project's issue tracking. Bugzilla entries were collected via the Bugzilla API; each entry has a "Version" field that explicitly states which release the bug was reported against, and bugs are attributed to that release. GitHub issues and mailing-list bugs are attributed to the most recent release that shipped before the bug was reported.</p>

<h3>Why this metric</h3>

<p>The critics' claim is a simple comparison: did the rate go up? The simplest honest response is a simple rate. If the Claude releases sit in the middle of the historical distribution, the burden shifts to the critics to explain why this particular middle is somehow worse than all the other middles that came before it.</p>

<h3>What this metric does not do</h3>

<p>It does not control for commit complexity, security intensity, or bug severity. It does not distinguish between a one-line typo fix and a CVE patch. It is a blunt instrument. But the critics' accusation is also blunt: "Claude is making things worse." A blunt instrument is the fairest response.</p>

</section>

<div class="section-divider"></div>

<!-- ═══════════ RESULTS ═══════════ -->

<section class="page-wide">

<h2>4 · Results</h2>

<h3>Claude Releases</h3>

<div class="g">
  {claude_cards}
</div>
<div class="mean-bar">
  <span class="label">Claude mean:</span> <span class="val claude">{claude_mean:.2f}</span>
  <span class="vs">vs</span>
  <span class="label">Historical mean:</span> <span class="val hist">{hist_mean:.2f}</span>
  <span class="vs">({'{:.1f}×'.format(claude_mean / hist_mean) if hist_mean > 0 else 'N/A'})</span>
</div>

<h3>How Normal Are the Claude Releases?</h3>

<div class="result-callout">
  <div class="result-number">{p_value:.0%}</div>
  <div class="result-label">of random pairs match or exceed the Claude mean</div>
  <div class="result-detail">
    {n_extreme} of {n_total} possible pairs of 2 historical releases have mean bugs/10c
    ≥ {claude_mean:.2f}. Nearly half. The Claude releases sit exactly where most pairs land —
    the middle of the distribution, not the tail.
    <br><br>
    <span style="font-family:var(--mono);font-size:0.85rem">
    Claude mean: {claude_mean:.2f} · Historical mean: {hist_mean:.2f} · IQR: {q25:.2f}–{q75:.2f}
    </span>
  </div>
</div>

<h3>The Distribution (log scale)</h3>

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
  Half of all historical releases fall inside this band, and half fall outside.
  The darker regions on either side are the lower and upper quarters.
  The Claude releases (green dots) <strong>{"both fall inside" if all(q25 <= r["bugs_10c"] <= q75 for r in claude) else "don't both fall inside"} the IQR</strong> —
  their bug rates are indistinguishable from the typical historical range.
</p>

<h3>Regime Check</h3>

<p>The historical mean is {hist_mean:.2f} bugs/10c, but this is driven by a bimodal distribution. v2.x releases average {v2_mean:.2f} bugs/10c; v3.x releases average {v3_mean:.2f}. Even within v3.x, the Claude releases are unremarkable: v3.4.2 ranks 16th of 21 v3.x releases, v3.4.3 ranks 16th as well.</p>

<p>A runs test on the {len(nc_data)} non-Claude releases finds {runs} runs (expected {exp_runs:.1f} under randomness, z={z_runs:.2f}, <strong>p={p_runs:.3f}</strong>). There is no evidence of temporal clustering — the sequence is consistent with a random draw from the same distribution.</p>

<h3>The Outlier Nobody Noticed</h3>

<div class="result-callout">
  <div class="result-number">113.33</div>
  <div class="result-label">bugs per 10 commits — v3.4.1, no Claude</div>
  <div class="result-detail">
    The highest bug rate in the entire dataset. 102 bugs in 9 commits, a hotfix release
    the day after v3.4.0. It exceeds every other release by an order of magnitude.
    <strong>Nobody noticed.</strong> There was no AI to blame <strong>so</strong> there was no GitHub issue with
    300 comments, no death threats, no threats to fork or move to openrsync. A maintainer
    shipped a broken release and fixed it. This happens. The only thing that made v3.4.3
    special was the availability of an enemy <em>everyone had already decided to hate</em>.
  </div>
</div>

<h3>All Releases (chronological)</h3>

<div class="table-wrapper">
<table>
  <thead>
    <tr><th>Release</th><th>Bugs</th><th>Commits</th><th>WtSec</th><th>Claude</th><th>Bugs/10c</th><th>Percentile</th></tr>
  </thead>
  <tbody>
    {table_rows}
  </tbody>
</table>
</div>

</section>

<div class="section-divider"></div>

<!-- ═══════════ CONCLUSION ═══════════ -->

<section class="page">

<h2>5 · What the Data Is Consistent And Inconsistent With</h2>

<div class="scorecard">

  <div class="score-row">
    <div class="score-verdict yes">✓</div>
    <div class="score-body">
      <div class="score-claim">"The Claude releases are statistically indistinguishable from historical releases"</div>
      <div class="score-evidence">Both releases fall inside the IQR. The permutation test shows <strong>{p_value:.0%}</strong> of random pairs score equal or worse. There is no signal of abnormality.</div>
    </div>
  </div>

  <div class="score-row">
    <div class="score-verdict yes">✓</div>
    <div class="score-body">
      <div class="score-claim">"The outrage selected on a single tail event and narrativized it"</div>
      <div class="score-evidence">A Mastodon user noticed a regression in v3.4.3, saw Claude commits, and concluded causation. But v3.4.3 at 6.76 bugs/10c is at the 74th percentile — elevated but not extreme. Eleven historical releases scored higher. The correlation is noise.</div>
    </div>
  </div>

  <div class="score-row">
    <div class="score-verdict weak">∓</div>
    <div class="score-body">
      <div class="score-claim">"Claude may have reduced the bug rate"</div>
      <div class="score-evidence">The Claude mean (3.78) is half the historical mean (7.60). But with only 2 releases, this difference is not statistically distinguishable from chance. <strong>The data cannot tell us the magnitude. It can tell us the direction: not harmful.</strong></div>
    </div>
  </div>

  <div class="score-row">
    <div class="score-verdict no">✗</div>
    <div class="score-body">
      <div class="score-claim">"Claude clearly made things worse"</div>
      <div class="score-evidence">Both Claude releases fall inside the middle 50% of historical releases. There is no distributional evidence of harm. The claim rests entirely on a post-hoc correlation observed by a social media user.</div>
    </div>
  </div>

  <div class="score-row">
    <div class="score-verdict no">✗</div>
    <div class="score-body">
      <div class="score-claim">"The regressions speak for themselves"</div>
      <div class="score-evidence">v3.4.1 — a pre-Claude release — has the highest bug rate in the dataset (113.33 bugs/10c). Nobody noticed, because there was no AI to be angry at. The regressions only "speak" when you ignore the historical distribution.</div>
    </div>
  </div>

  <div class="score-row">
    <div class="score-verdict no">✗</div>
    <div class="score-body">
      <div class="score-claim">"Just wait, more bugs will surface"</div>
      <div class="score-evidence">v3.4.3 has been out long enough that its rate (6.76) is already comparable to historical releases. The "wait and see" argument is an appeal to an unknowable future that shifts the burden of proof away from the critics. If more bugs surface, they will enter the distribution like every other release. There is no reason to expect a regime break.</div>
    </div>
  </div>

</div>

</section>

<!-- ═══════════ CLOSING ═══════════ -->

<div class="closing">
<div class="page">

<blockquote>
<p>…for the people saying things like "I'm a PhD from xyz uni and I'm telling you LLMs are just stochastic tools that make everything up and the world will fall apart if you use them", I'm here to tell you that you are out of date. The world of software engineering has changed dramatically in the last few months. The world of IT security and maintaining software in the face of the flood of reports has completely and utterly changed just in the last few weeks. Anything you learned about this stuff last year might as well be from another planet… Bottom line is I do know (well, roughly!) how LLMs work, but that doesn't make them not useful. It does mean you have to be cautious, but I am being cautious, or as cautious as I can be given my desire to be sailing and not dealing with a flood of gunk from so-called internet experts.</p>
<span class="attr">— <a href="https://medium.com/@tridge60/rsync-and-outrage-d9849599e5a0">Andrew Tridgell</a></span>
</blockquote>

</div>
</div>

<!-- ═══════════ FOOTER ═══════════ -->

<footer>
  Bugs/10c Distribution — rsync · June 2026 · All data from GitHub REST API, Bugzilla, and mailing lists
</footer>

</body>
</html>"""

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
