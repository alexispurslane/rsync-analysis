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
import sys
from pathlib import Path
from string import Template

import duckdb
import numpy as np
from scipy.stats import fisher_exact

# Allow importing severity_scoring from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))
from severity_rubric import build_rubric_html, SEVERITY_RUBRIC, SEVERITY_RULES

DB_PATH = Path(__file__).resolve().parent / "rsync_github.duckdb"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs"

# ── Data loading ──

def load_data(con: duckdb.DuckDBPyConnection, filter_nonbugs: bool = False) -> list[dict]:
    if filter_nonbugs:
        # Re-count bugs excluding severity=0 (LLM-judged non-bugs).
        # Track github_total_filed so we can distinguish "all were non-bugs" from "no data".
        rows = con.execute("""
            WITH github_real AS (
                SELECT a.attributed_release AS tag_name,
                       COUNT(*) FILTER (WHERE b.severity IS NULL OR b.severity > 0) AS real_bugs,
                       COUNT(*) AS total_filed
                FROM bug_release_attribution a
                JOIN bugs b ON a.bug_number = b.number
                GROUP BY a.attributed_release
            ),
            filtered_bugs AS (
                SELECT tag_name,
                       SUM(CASE
                           WHEN source = 'github' THEN real_bugs
                           ELSE bug_count
                       END) AS bug_count,
                       MAX(CASE WHEN source = 'github' THEN total_filed ELSE 0 END) AS github_filed
                FROM (
                    SELECT tag_name, real_bugs AS bug_count, real_bugs, total_filed, 'github' AS source
                    FROM github_real
                    UNION ALL
                    SELECT tag_name, bug_count, NULL, 0, 'other' AS source
                    FROM release_bugs
                    WHERE source != 'github'
                )
                GROUP BY tag_name
            )
            SELECT rc.tag_name,
                   COALESCE(fb.bug_count, 0),
                   rc.total_commits, rc.wt_sec, rc.claude_commits,
                   COALESCE(fb.github_filed, 0) AS github_total_filed
            FROM release_commits rc
            LEFT JOIN filtered_bugs fb ON rc.tag_name = fb.tag_name
            WHERE rc.tag_name NOT LIKE 'mbp%'
            ORDER BY rc.tag_name
        """).fetchall()
    else:
        rows = con.execute("""
            SELECT tag_name, bug_count, total_commits, wt_sec, claude_commits,
                   0 AS github_total_filed
            FROM release_table
            WHERE tag_name NOT LIKE 'mbp%'
            ORDER BY tag_name
        """).fetchall()
    return [dict(zip(
        ["tag", "bugs", "commits", "wt_sec", "claude", "is_claude", "github_total_filed"],
        [r[0], r[1], r[2], float(r[3]), r[4], r[4] > 0, r[5]]
    )) for r in rows]


def load_severity_sums(con: duckdb.DuckDBPyConnection, filter_nonbugs: bool = False) -> dict[str, float]:
    """Return {tag: sum of (severity/100)} for each release.

    Normalizes 0–100 severity to 0.0–1.0, then sums per release.
    """
    if filter_nonbugs:
        github_where = "AND (b.severity IS NULL OR b.severity > 0)"
        bz_where = "AND severity > 0"
        ml_where = "AND severity > 0"
    else:
        github_where = ""
        bz_where = ""
        ml_where = ""

    rows = con.execute(f"""
        -- GitHub bugs
        SELECT
            a.attributed_release AS tag_name,
            SUM(b.severity / 100.0) AS weighted
        FROM bug_release_attribution a
        JOIN bugs b ON a.bug_number = b.number
        WHERE 1=1 {github_where}
        GROUP BY a.attributed_release

        UNION ALL

        -- Bugzilla bugs
        SELECT
            'v' || version AS tag_name,
            SUM(severity / 100.0) AS weighted
        FROM bugzilla_bugs
        WHERE version IS NOT NULL
          AND version != ''
          AND version != 'unspecified'
          AND resolution NOT IN ('DUPLICATE', 'INVALID', 'WONTFIX', 'WORKSFORME')
          {bz_where}
        GROUP BY version

        UNION ALL

        -- Mailing list bugs
        SELECT
            CASE WHEN version LIKE 'v%' THEN version ELSE 'v' || version END AS tag_name,
            SUM(severity / 100.0) AS weighted
        FROM ml_bug_reports
        WHERE version IS NOT NULL
          {ml_where}
        GROUP BY tag_name
    """).fetchall()

    # Aggregate across sources
    result: dict[str, float] = {}
    for tag, weighted in rows:
        result[tag] = result.get(tag, 0.0) + float(weighted)
    return result

# ── Stats helpers ──

def ordinal(n: int) -> str:
    """Return ordinal string: 1st, 2nd, 3rd, 31st, etc."""
    if 11 <= n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

def log_pct(rate: float) -> float:
    """Map sev/10c to % position on a log scale (0.01→300 = 0→100%)."""
    if rate <= 0:
        return 0
    return (np.log10(rate) + 2) / 4.5 * 100

# ── HTML generation ──

def generate_report(releases: list[dict], severity_sums: dict[str, float] | None = None, severity_examples: str = "", tag_diffs: dict[str, int] | None = None) -> str:
    # Inclusion logic:
    #   - Always include GitHub-era releases (v3.2.0+) that have commits —
    #     if they have 0 bugs that's signal, not missing data.
    #   - Pre-GitHub releases: only include if they have bugs > 0 —
    #     if bugs=0 that means we lack data, not that there were none.
    github_era = {r["tag"] for r in releases
                   if r["commits"] > 0 and r["tag"].startswith("v3.")
                   and not r["tag"].startswith("v3.0") and not r["tag"].startswith("v3.1.")}
    with_data = [r for r in releases if r["commits"] > 0 and (r["bugs"] > 0 or r["tag"] in github_era)]
    for r in with_data:
        r["bugs_10c"] = r["bugs"] * 10 / r["commits"]
        sev_sum = severity_sums.get(r["tag"], 0.0) if severity_sums else 0.0
        r["sev_10c"] = sev_sum * 10 / r["commits"]
        r["sev_sum"] = sev_sum

    # The primary metric is severity-weighted: sev_10c
    # bugs_10c is kept for the table only.
    RATE = "sev_10c"

    historical = [r for r in with_data if not r["is_claude"]]
    claude = [r for r in with_data if r["is_claude"]]
    hist_rates = sorted(r[RATE] for r in historical)
    claude_mean = np.mean([r[RATE] for r in claude])
    hist_mean = np.mean(hist_rates)

    for r in claude:
        r["percentile"] = np.searchsorted(hist_rates, r[RATE]) / len(hist_rates) * 100
        r["rank"] = sum(1 for h in hist_rates if h <= r[RATE])
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
    hist_only_rates = [r[RATE] for r in hist_only]
    n_hist = len(hist_only)
    n_extreme = sum(
        1 for combo in combinations(range(n_hist), k_bug)
        if np.mean([hist_only_rates[i] for i in combo]) >= claude_mean
    )
    n_total = len(list(combinations(range(n_hist), k_bug)))
    p_value = n_extreme / n_total

    # Fisher's exact test: 2×2 table of Claude/non-Claude × above/below historical median
    fisher_median = np.median(hist_rates)
    fisher_a = sum(1 for r in with_data if r[RATE] <= fisher_median and not r["is_claude"])
    fisher_b = sum(1 for r in with_data if r[RATE] > fisher_median and not r["is_claude"])
    fisher_c = sum(1 for r in with_data if r[RATE] <= fisher_median and r["is_claude"])
    fisher_d = sum(1 for r in with_data if r[RATE] > fisher_median and r["is_claude"])
    fisher_table = np.array([[fisher_a, fisher_b], [fisher_c, fisher_d]])
    fisher_oddsratio, fisher_p = fisher_exact(fisher_table, alternative='greater')

    claude_ranks = []
    for r in claude:
        rank = sum(1 for h in hist_rates if h <= r[RATE])
        claude_ranks.append((r["tag"], r[RATE], rank, len(hist_rates)))

    # Regime comparison: v2.x vs v3.x
    v2_releases = [r for r in with_data if r["tag"].startswith("v2.")]
    v3_releases = [r for r in with_data if r["tag"].startswith("v3.")]
    v2_mean = np.mean([r[RATE] for r in v2_releases])
    v3_mean = np.mean([r[RATE] for r in v3_releases])

    # Runs test on non-Claude releases
    nc_data = [r for r in with_data if not r["is_claude"]]
    nc_rates_only = [r[RATE] for r in nc_data]
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
        left = log_pct(r[RATE])
        dot_class = "dot claude-dot" if is_c else "dot"
        strip_parts.append(
            f'<div class="{dot_class}" '
            f'style="left:{left:.1f}%;background:{color};width:{size}px;height:{size}px" '
            f'title="{r["tag"]}: {r[RATE]:.2f} sev/10c"></div>'
        )
        if is_c:
            in_iqr = q25 <= r[RATE] <= q75
            badge_html = '<span class="badge">inside middle 50% ✓</span>' if in_iqr else ''
            strip_parts.append(
                f'<div class="dot-tag" style="left:{left:.1f}%">'
                f'{r["tag"]}'
                f'{badge_html}</div>'
            )

    strip_items = "\n  ".join(strip_parts)

    # Claude cards
    claude_cards = ""
    for r in claude:
        pctile_int = int(round(r["percentile"]))
        claude_cards += (
            f'<div class="c">'
            f'<h3>{r["tag"]}</h3>'
            f'<div class="b">{r[RATE]:.2f} <span class="u">sev/10c</span></div>'
            f'<div class="d">{r["bugs"]} bugs · {r["commits"]} commits · {r["claude"]} Claude</div>'
            f'<div class="d pctile">{ordinal(pctile_int)} percentile (rank {r["rank"]} of {r["out_of"]})</div>'
            f'</div>'
        )

    # Percentiles for all releases (against historical distribution)
    for r in sorted_data:
        if "percentile" not in r:
            r["percentile"] = np.searchsorted(hist_rates, r[RATE]) / len(hist_rates) * 100

    # Table rows
    table_rows = ""
    for r in sorted_data:
        is_c = r["is_claude"]
        pctile = f'{ordinal(int(round(r["percentile"])))} percentile'
        table_rows += (
            f'<tr class="{"claude-era" if is_c else ""}">'
            f'<td class="rel">{r["tag"]}</td>'
            f'<td class="n">{r["bugs"]}</td>'
            f'<td class="n">{r["sev_sum"]:.1f}</td>'
            f'<td class="n">{r["commits"]}</td>'
            f'<td class="n">{r["claude"]}</td>'
            f'<td class="n rate">{r["bugs_10c"]:.2f}</td>'
            f'<td class="n rate">{r[RATE]:.2f}</td>'
            f'<td class="era">{pctile}</td></tr>\n          '
        )

    # Load template
    template_path = Path(__file__).resolve().parent / "bug_rate_report.html"
    template = Template(template_path.read_text())

    # Compute template variables
    claude_in_iqr_parts = []
    for r in claude:
        if r[RATE] < q25:
            claude_in_iqr_parts.append(f"{r['tag']} is just below the IQR ({r[RATE]:.2f} sev/10c, IQR starts at {q25:.2f})")
        elif r[RATE] > q75:
            claude_in_iqr_parts.append(f"{r['tag']} is just above the IQR ({r[RATE]:.2f} sev/10c, IQR ends at {q75:.2f})")
        else:
            claude_in_iqr_parts.append(f"{r['tag']} is inside the IQR ({r[RATE]:.2f} sev/10c)")

    if all(q25 <= r[RATE] <= q75 for r in claude):
        claude_in_iqr = "both fall inside"
        claude_iqr_summary = "Both Claude releases fall inside the middle 50% of the historical distribution."
    elif any(r[RATE] < q25 for r in claude) and any(r[RATE] > q75 for r in claude):
        claude_in_iqr = "bracket the IQR in opposite directions"
        below = [r for r in claude if r[RATE] < q25]
        above = [r for r in claude if r[RATE] > q75]
        claude_iqr_summary = (
            f"The Claude releases bracket the IQR in opposite directions: "
            f"{', '.join(r['tag'] for r in below)} "
            f"{'is' if len(below) == 1 else 'are'} below the IQR, "
            f"{', '.join(r['tag'] for r in above)} "
            f"{'is' if len(above) == 1 else 'are'} above it. "
            f"Neither is an outlier."
        )
    else:
        claude_in_iqr = "; ".join(claude_in_iqr_parts)
        claude_iqr_summary = "; ".join(claude_in_iqr_parts)

    # ── Outlier release (highest bug rate, no Claude) ──
    non_claude_with_data = [r for r in with_data if not r["is_claude"]]
    outlier = max(non_claude_with_data, key=lambda r: r[RATE])
    all_sorted_tags = [r["tag"] for r in sorted_data]
    outlier_idx = all_sorted_tags.index(outlier["tag"])
    outlier_prev_tag = all_sorted_tags[outlier_idx - 1] if outlier_idx > 0 else "the prior release"

    # ── Claude "worst" release (higher bug rate among Claude releases) ──
    claude_worst = max(claude, key=lambda r: r[RATE])
    claude_worst_rank = sum(1 for h in hist_rates if h <= claude_worst[RATE])
    claude_worst_pctile = int(round(claude_worst_rank / len(hist_rates) * 100))
    n_higher_than_worst = sum(1 for h in hist_rates if h > claude_worst[RATE])

    # ── v3.x strip chart ──
    v3_with_data = sorted(
        [r for r in with_data if r["tag"].startswith("v3.")],
        key=lambda x: x["tag"],
    )
    n_v3 = len(v3_with_data)
    v3_rates = sorted(r[RATE] for r in v3_with_data if not r["is_claude"])
    v3_q25 = np.percentile(v3_rates, 25)
    v3_q75 = np.percentile(v3_rates, 75)
    v3_median = np.median(v3_rates)
    v3_q25_left = log_pct(v3_q25)
    v3_q75_left = log_pct(v3_q75)

    v3_strip_parts = [
        f'<div class="outside" style="right:{100 - v3_q25_left:.1f}%"></div>',
        f'<div class="outside" style="left:{v3_q75_left:.1f}%"></div>',
        f'<div class="iqr" style="left:{v3_q25_left:.1f}%;width:{v3_q75_left - v3_q25_left:.1f}%"></div>',
        f'<div class="iqr-center" style="left:{(v3_q25_left + v3_q75_left) / 2:.1f}%">middle 50%</div>',
        f'<div class="med" style="left:{log_pct(v3_median):.1f}%"></div>',
    ]
    for r in v3_with_data:
        is_c = r["is_claude"]
        color = "#2d6a4f" if is_c else "#b44a1e"
        size = 18 if is_c else 11
        left = log_pct(r[RATE])
        dot_class = "dot claude-dot" if is_c else "dot"
        v3_strip_parts.append(
            f'<div class="{dot_class}" '
            f'style="left:{left:.1f}%;background:{color};width:{size}px;height:{size}px" '
            f'title="{r["tag"]}: {r[RATE]:.2f} sev/10c"></div>'
        )
        if is_c:
            in_v3_iqr = v3_q25 <= r[RATE] <= v3_q75
            badge_html = '<span class="badge">inside middle 50% ✓</span>' if in_v3_iqr else ''
            v3_strip_parts.append(
                f'<div class="dot-tag" style="left:{left:.1f}%">'
                f'{r["tag"]}'
                f'{badge_html}</div>'
            )
    v3_strip_items = "\n  ".join(v3_strip_parts)

    # ── Releases with bug data ──
    releases_with_bugs = [r for r in releases if r["bugs"] > 0]
    first_release_tag = releases_with_bugs[0]["tag"] if releases_with_bugs else ""
    last_release_tag = releases_with_bugs[-1]["tag"] if releases_with_bugs else ""

    # ── Claude summary line for executive summary ──
    claude_summary_parts = [
        f"{cr['tag']} ({cr['claude']} Claude, {cr[RATE]:.2f} sev/10c)"
        for cr in sorted(claude, key=lambda x: x["tag"])
    ]
    claude_summary_line = " and ".join(claude_summary_parts)

    # ── Historical vs Claude mean ratio ──
    if claude_mean > 0:
        hist_claude_ratio = hist_mean / claude_mean
        if abs(hist_claude_ratio - round(hist_claude_ratio)) < 0.15:
            hist_claude_ratio_str = f"{round(hist_claude_ratio):d}\u00d7"
        else:
            hist_claude_ratio_str = f"{hist_claude_ratio:.1f}\u00d7"
        if 1.5 <= hist_claude_ratio < 2.5:
            hist_claude_ratio_desc_str = "half"
        elif hist_claude_ratio < 1.5:
            hist_claude_ratio_desc_str = "close to"
        else:
            hist_claude_ratio_desc_str = f"{1/hist_claude_ratio:.0%} of"
    else:
        hist_claude_ratio_str = "\u221e\u00d7"
        hist_claude_ratio_desc_str = "infinitely higher than"

    # ── Commit rate permutation test ──
    hist_commit_rates = [r["commits"] for r in hist_only]
    claude_commit_mean = np.mean([r["commits"] for r in claude])
    hist_commit_mean = np.mean(hist_commit_rates)
    n_commit_extreme = sum(
        1 for combo in combinations(range(len(hist_commit_rates)), k_bug)
        if np.mean([hist_commit_rates[i] for i in combo]) >= claude_commit_mean
    )
    commit_perm_p = n_commit_extreme / n_total

    # ── Lines changed permutation test ──
    if tag_diffs:
        for r in with_data:
            r["changes"] = tag_diffs.get(r["tag"])
        has_changes = [r for r in with_data if r["changes"] is not None]
        hist_changes = [r["changes"] for r in has_changes if not r["is_claude"]]
        claude_changes = [r["changes"] for r in has_changes if r["is_claude"]]
        if claude_changes:
            claude_lines_mean = np.mean(claude_changes)
            hist_lines_mean = np.mean(hist_changes)
            n_lines_extreme = sum(
                1 for combo in combinations(range(len(hist_changes)), k_bug)
                if np.mean([hist_changes[i] for i in combo]) >= claude_lines_mean
            )
            lines_perm_p = n_lines_extreme / n_total
        else:
            claude_lines_mean = 0
            hist_lines_mean = np.mean(hist_changes) if hist_changes else 0
            lines_perm_p = 1.0
    else:
        claude_lines_mean = 0
        hist_lines_mean = 0
        lines_perm_p = 1.0

    # ── Absolute severity-weighted bug count permutation test ──
    hist_sev = [r["sev_sum"] for r in with_data if not r["is_claude"]]
    claude_sev = [r["sev_sum"] for r in claude]
    claude_sev_mean = np.mean(claude_sev)
    hist_sev_mean = np.mean(hist_sev)
    n_sev_extreme = sum(
        1 for combo in combinations(range(len(hist_sev)), k_bug)
        if np.mean([hist_sev[i] for i in combo]) >= claude_sev_mean
    )
    sev_perm_p = n_sev_extreme / n_total

    html = template.substitute(
        severity_rubric_table=build_rubric_html(),
        severity_examples=severity_examples,
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
        claude_iqr_summary=claude_iqr_summary,
        v2_mean_str=f"{v2_mean:.2f}",
        v3_mean_str=f"{v3_mean:.2f}",
        nc_count=len(nc_data),
        runs=runs,
        exp_runs_str=f"{exp_runs:.1f}",
        z_runs_str=f"{z_runs:.2f}",
        p_runs_str=f"{p_runs:.3f}",
        table_rows=table_rows,
        fisher_p_str=f"{fisher_p:.0%}",
        fisher_oddsratio_str=f"{fisher_oddsratio:.2f}",
        fisher_a=fisher_a,
        fisher_b=fisher_b,
        fisher_c=fisher_c,
        fisher_d=fisher_d,
        fisher_median_str=f"{fisher_median:.2f}",
        # New dynamic template variables
        n_releases_with_bugs=len(releases_with_bugs),
        first_release_tag=first_release_tag,
        last_release_tag=last_release_tag,
        n_claude_releases=len(claude),
        claude_summary_line=claude_summary_line,
        hist_claude_ratio_str=hist_claude_ratio_str,
        hist_claude_ratio_desc_str=hist_claude_ratio_desc_str,
        outlier_tag=outlier["tag"],
        outlier_rate_str=f"{outlier[RATE]:.2f}",
        outlier_bugs=outlier["bugs"],
        outlier_commits=outlier["commits"],
        outlier_prev_tag=outlier_prev_tag,
        claude_worst_tag=claude_worst["tag"],
        claude_worst_rate_str=f"{claude_worst[RATE]:.2f}",
        claude_worst_pctile=claude_worst_pctile,
        claude_worst_pctile_str=ordinal(claude_worst_pctile),
        n_higher_than_worst_claude=n_higher_than_worst,
        commit_perm_p_str=f"{commit_perm_p:.0%}",
        claude_commit_mean_str=f"{claude_commit_mean:.0f}",
        hist_commit_mean_str=f"{hist_commit_mean:.0f}",
        lines_perm_p_str=f"{lines_perm_p:.0%}",
        claude_lines_mean_str=f"{claude_lines_mean:.0f}",
        hist_lines_mean_str=f"{hist_lines_mean:.0f}",
        sev_perm_p_str=f"{sev_perm_p:.0%}",
        claude_sev_mean_str=f"{claude_sev_mean:.1f}",
        hist_sev_mean_str=f"{hist_sev_mean:.1f}",
        claude_v3_ranks=v3_strip_items,
        n_v3=n_v3,
    )

    return html

def build_severity_examples(con: duckdb.DuckDBPyConnection) -> str:
    """Build example scoring rows from the DB, one from each rubric tier."""
    # Pick one illustrative example per severity band, preferring Claude releases
    examples = []
    targets = [(95, 100), (75, 89), (55, 69), (35, 49), (15, 29), (0, 0)]
    for target_lo, target_hi in targets:
        row = con.execute("""
            SELECT b.number, b.title, b.severity, a.attributed_release, b.url
            FROM bugs b
            JOIN bug_release_attribution a ON b.number = a.bug_number
            WHERE b.severity IS NOT NULL AND b.severity BETWEEN ? AND ?
            ORDER BY CASE WHEN b.severity = ? THEN 0 ELSE 1 END, b.number DESC
            LIMIT 1
        """, [target_lo, target_hi, target_lo]).fetchone()
        if row:
            examples.append(row)
    rows_html = []
    for r in examples:
        rows_html.append(
            f'<tr>'
            f'<td class="rubric-range">{r[2]}</td>'
            f'<td><a href="{r[4]}">{r[3]}</a></td>'
            f'<td>{r[1][:100]}</td>'
            f'</tr>'
        )
    header = '<tr><th>Score</th><th>Release</th><th>Title</th></tr>'
    return f'<table class="rubric-table scoring-examples">\n<thead>{header}</thead>\n<tbody>{"".join(rows_html)}</tbody>\n</table>'


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Generate rsync bug-rate analysis report")
    parser.add_argument("--unfiltered", action="store_true",
                        help="Include bugs scored severity=0 by the LLM (feature requests, spam, etc.)")
    args = parser.parse_args()
    filter_nonbugs = not args.unfiltered

    con = duckdb.connect(str(DB_PATH), read_only=True)
    releases = load_data(con, filter_nonbugs=filter_nonbugs)
    severity_sums = load_severity_sums(con, filter_nonbugs=filter_nonbugs)
    severity_examples = build_severity_examples(con)
    # Load line-change data from tag_diff_stats
    try:
        tag_diffs = {r[0]: r[1] for r in con.execute("SELECT tag_name, changes FROM tag_diff_stats").fetchall()}
    except Exception:
        tag_diffs = {}
    con.close()

    html = generate_report(releases, severity_sums=severity_sums, severity_examples=severity_examples, tag_diffs=tag_diffs)
    OUTPUT_DIR.mkdir(exist_ok=True)

    out_name = "index.html" if filter_nonbugs else "index_unfiltered.html"
    (OUTPUT_DIR / out_name).write_text(html)
    print(f"Written to {OUTPUT_DIR / out_name}")

    # Copy CSS to output dir
    css_src = Path(__file__).resolve().parent / "bug_rate_report.css"
    css_dst = OUTPUT_DIR / "bug_rate_report.css"
    css_dst.write_text(css_src.read_text())


if __name__ == "__main__":
    main()
