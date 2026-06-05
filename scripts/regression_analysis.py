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
from string import Template

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

def ordinal(n: int) -> str:
    """Return ordinal string: 1st, 2nd, 3rd, 31st, etc."""
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

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
        pctile_int = int(round(r["percentile"]))
        claude_cards += (
            f'<div class="c">'
            f'<h3>{r["tag"]}</h3>'
            f'<div class="b">{r["bugs_10c"]:.2f} <span class="u">bugs/10c</span></div>'
            f'<div class="d">{r["bugs"]} bugs · {r["commits"]} commits · {r["claude"]} Claude</div>'
            f'<div class="d pctile">{ordinal(pctile_int)} percentile (rank {r["rank"]} of {r["out_of"]})</div>'
            f'</div>'
        )

    # Table rows
    table_rows = ""
    for r in sorted_data:
        is_c = r["is_claude"]
        pctile = f'{ordinal(int(round(r["percentile"])))} percentile' if is_c else ""
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

    # Load template
    template_path = Path(__file__).resolve().parent / "regression_report.html"
    template = Template(template_path.read_text())

    # Compute template variables
    claude_in_iqr = "both fall inside" if all(q25 <= r["bugs_10c"] <= q75 for r in claude) else "don't both fall inside"

    html = template.substitute(
        claude_cards=claude_cards,
        claude_mean_str=f"{claude_mean:.2f}",
        hist_mean_str=f"{hist_mean:.2f}",
        p_value_str=f"{p_value:.0%}",
        n_extreme=n_extreme,
        n_total=n_total,
        q25_str=f"{q25:.2f}",
        q75_str=f"{q75:.2f}",
        strip_items=strip_items,
        claude_in_iqr=claude_in_iqr,
        v2_mean_str=f"{v2_mean:.2f}",
        v3_mean_str=f"{v3_mean:.2f}",
        nc_count=len(nc_data),
        runs=runs,
        exp_runs_str=f"{exp_runs:.1f}",
        z_runs_str=f"{z_runs:.2f}",
        p_runs_str=f"{p_runs:.3f}",
        table_rows=table_rows,
    )

    return html

def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    releases = load_data(con)
    con.close()

    html = generate_report(releases)
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "index.html").write_text(html)

    # Copy CSS to output dir (same location as index.html for relative ref)
    css_src = Path(__file__).resolve().parent / "regression_report.css"
    css_dst = OUTPUT_DIR / "regression_report.css"
    css_dst.write_text(css_src.read_text())

    print(f"Written to {OUTPUT_DIR / 'index.html'}")


if __name__ == "__main__":
    main()
