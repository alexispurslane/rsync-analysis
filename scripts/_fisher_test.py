#!/usr/bin/env python3
"""
One-off: compare the current permutation test with Fisher's exact test.

Current approach: what fraction of random k-subsets of historical releases
have mean bugs/10c >= the Claude mean?

Fisher's exact test: 2×2 contingency table of Claude/non-Claude ×
above/below the historical median bug rate, testing whether Claude releases
are independent of being above/below median.
"""

import duckdb
import numpy as np
from scipy.stats import fisher_exact
from itertools import combinations
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "rsync_github.duckdb"

con = duckdb.connect(str(DB_PATH), read_only=True)
rows = con.execute("""
    SELECT tag_name, bug_count, total_commits, wt_sec, claude_commits
    FROM release_table
    WHERE tag_name NOT LIKE 'mbp%'
    ORDER BY tag_name
""").fetchall()
con.close()

releases = [dict(zip(
    ["tag", "bugs", "commits", "wt_sec", "claude", "is_claude"],
    [r[0], r[1], r[2], float(r[3]), r[4], r[4] > 0]
)) for r in rows]

with_data = [r for r in releases if r["bugs"] > 0 and r["commits"] > 0]
for r in with_data:
    r["bugs_10c"] = r["bugs"] * 10 / r["commits"]

historical = [r for r in with_data if not r["is_claude"]]
claude = [r for r in with_data if r["is_claude"]]
hist_rates = sorted(r["bugs_10c"] for r in historical)
claude_mean = np.mean([r["bugs_10c"] for r in claude])
hist_mean = np.mean(hist_rates)
median = np.median(hist_rates)

# ── Current permutation test ──
k_bug = len(claude)
hist_only_rates = [r["bugs_10c"] for r in historical]
n_hist = len(historical)
n_extreme = sum(
    1 for combo in combinations(range(n_hist), k_bug)
    if np.mean([hist_only_rates[i] for i in combo]) >= claude_mean
)
n_total = len(list(combinations(range(n_hist), k_bug)))
p_perm = n_extreme / n_total

print("=== Current Permutation Test ===")
print(f"  Claude mean: {claude_mean:.2f} bugs/10c")
print(f"  Historical mean: {hist_mean:.2f} bugs/10c")
print(f"  {n_extreme} of {n_total} random {k_bug}-subsets >= Claude mean")
print(f"  p = {p_perm:.4f} ({p_perm:.0%})")

# ── Fisher's exact test ──
# 2×2 table: Claude/non-Claude × above/below historical median
# H0: Claude status and being above median are independent

above = [r for r in with_data if r["bugs_10c"] > median]
below = [r for r in with_data if r["bugs_10c"] <= median]

# Table layout:
#              | <= median | > median |
#  non-Claude  |    a      |    b     |
#  Claude      |    c      |    d     |

a = sum(1 for r in below if not r["is_claude"])
b = sum(1 for r in above if not r["is_claude"])
c = sum(1 for r in below if r["is_claude"])
d = sum(1 for r in above if r["is_claude"])

table = np.array([[a, b], [c, d]])

print(f"\n=== Fisher's Exact Test ===")
print(f"  Median historical bug rate: {median:.2f} bugs/10c")
print(f"  Contingency table (rows: non-Claude/Claude, cols: ≤median/>median):")
print(f"              ≤ median   > median")
print(f"  non-Claude    {a:3d}        {b:3d}")
print(f"  Claude        {c:3d}        {d:3d}")

oddsratio, p_fisher = fisher_exact(table, alternative='greater')
print(f"\n  Fisher's exact test (one-sided, H1: Claude more likely > median):")
print(f"    Odds ratio: {oddsratio:.4f}")
print(f"    p = {p_fisher:.4f} ({p_fisher:.0%})")

oddsratio_two, p_fisher_two = fisher_exact(table)
print(f"\n  Fisher's exact test (two-sided):")
print(f"    Odds ratio: {oddsratio_two:.4f}")
print(f"    p = {p_fisher_two:.4f} ({p_fisher_two:.0%})")

# ── Also try Fisher with mean as threshold ──
mean_thresh = hist_mean
above_m = [r for r in with_data if r["bugs_10c"] > mean_thresh]
below_m = [r for r in with_data if r["bugs_10c"] <= mean_thresh]

a2 = sum(1 for r in below_m if not r["is_claude"])
b2 = sum(1 for r in above_m if not r["is_claude"])
c2 = sum(1 for r in below_m if r["is_claude"])
d2 = sum(1 for r in above_m if r["is_claude"])

table2 = np.array([[a2, b2], [c2, d2]])

print(f"\n=== Fisher's Exact Test (threshold = historical mean {mean_thresh:.2f}) ===")
print(f"              ≤ mean     > mean")
print(f"  non-Claude    {a2:3d}        {b2:3d}")
print(f"  Claude        {c2:3d}        {d2:3d}")

oddsratio2, p_fisher2 = fisher_exact(table2, alternative='greater')
print(f"\n  One-sided (H1: Claude more likely > mean):")
print(f"    Odds ratio: {oddsratio2:.4f}")
print(f"    p = {p_fisher2:.4f} ({p_fisher2:.0%})")

# ── Comparison ──
print(f"\n=== Comparison ===")
print(f"  Permutation test p:       {p_perm:.4f} ({p_perm:.0%})")
print(f"  Fisher (median) p:        {p_fisher:.4f} ({p_fisher:.0%})")
print(f"  Fisher (mean) p:          {p_fisher2:.4f} ({p_fisher2:.0%})")
print(f"  All say the same thing: {'YES' if all(p > 0.05 for p in [p_perm, p_fisher, p_fisher2]) else 'NO'}")
