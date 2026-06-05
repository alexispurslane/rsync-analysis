#!/usr/bin/env python3
"""
Rebuild analytical views in rsync_github.duckdb.

Core methodology: one row per release, five columns:

  1. tag_name        — release identifier (e.g. "v3.4.3")
  2. bug_count       — total bugs filed against this release (all sources)
  3. total_commits   — commits in this release's range
  4. wt_sec          — weighted security exposure (T1×1.0 + T2×0.4)
  5. claude_commits  — commits with Claude co-authorship

Bug counting rules:
  - Bugzilla: all bugs with version field match, excluding
    DUPLICATE/INVALID/WONTFIX/WORKSFORME (not real bugs)
  - GitHub:   all bugs attributed to the release by filing date
  - ML:       all bug reports (strict-filtered by fetch_mailinglist_bugs.py)
  - Sources are summed; overlap between GitHub and Bugzilla is small
    (v3.1.3, v3.2.0) and counts from both are included since they
    track different populations anyway

Some releases have bugs but no commit data (Bugzilla-only versions
not present in git: v2.5.7, v2.6.6, v3.0.4–v3.0.9). These appear
with NULL commit columns.

Supporting views (commits_with_ai, contributor_summary, etc.) are
retained as they feed into the release_table aggregates.
"""

from pathlib import Path

import duckdb

DB_PATH = Path(__file__).parent / "rsync_github.duckdb"


def rebuild_views(con: duckdb.DuckDBPyConnection) -> None:
    """Drop and recreate all analytical views."""

    # ── 1. commits_with_ai ──
    # Per-commit: Claude detection + tiered security classification.
    con.execute("DROP VIEW IF EXISTS commits_with_ai")
    con.execute("""
        CREATE VIEW commits_with_ai AS
        SELECT
            c.*,
            CASE
                WHEN c.message LIKE '%Co-Authored-By: Claude%' THEN TRUE
                ELSE FALSE
            END AS is_claude_assisted,
            CASE
                WHEN c.message LIKE '%Co-Authored-By: Claude Opus%' THEN 'Claude Opus'
                WHEN c.message LIKE '%Co-Authored-By: Claude Sonnet%' THEN 'Claude Sonnet'
                ELSE NULL
            END AS ai_model,
            CASE
                WHEN lower(c.message) LIKE '%cve-%'
                    OR lower(c.message) LIKE '%cve_%'
                    OR lower(c.message) LIKE '%toctou%'
                    OR lower(c.message) LIKE '%exploit%'
                    OR lower(c.message) LIKE '%harden%'
                    OR lower(c.message) LIKE '%openat2%'
                    OR lower(c.message) LIKE '%resolve_beneath%'
                    OR lower(c.message) LIKE '%vulnerability%'
                THEN 'T1'
                WHEN lower(c.message) LIKE '%security%'
                    OR lower(c.message) LIKE '%secure%'
                    OR lower(c.message) LIKE '%overflow%'
                    OR lower(c.message) LIKE '%bounds%'
                    OR lower(c.message) LIKE '%guard%'
                    OR lower(c.message) LIKE '%defence%'
                    OR lower(c.message) LIKE '%defense%'
                THEN 'T2'
                WHEN lower(c.message) LIKE '%symlink%'
                    OR lower(c.message) LIKE '%chroot%'
                    OR lower(c.message) LIKE '%escape%'
                    OR lower(c.message) LIKE '%restrict%'
                THEN 'T3'
                ELSE NULL
            END AS security_tier
        FROM commits AS c
    """)

    # ── 2. bug_release_attribution ──
    con.execute("DROP VIEW IF EXISTS bug_release_attribution")
    con.execute("""
        CREATE VIEW bug_release_attribution AS
        SELECT
            b.number AS bug_number,
            b.title AS bug_title,
            b.created_at,
            b.labels,
            (
                SELECT tr.tag_name
                FROM tag_ranges AS tr
                WHERE tr.tag_name NOT LIKE '%pre%'
                  AND tr.tag_name NOT LIKE '%rc%'
                  AND tr.tag_commit_date <= b.created_at
                ORDER BY tr.tag_commit_date DESC
                LIMIT 1
            ) AS attributed_release
        FROM bugs AS b
    """)

    # ── 3. release_commits ──
    # Per-release commit aggregates (git tags only).
    con.execute("DROP VIEW IF EXISTS release_commits")
    con.execute("""
        CREATE VIEW release_commits AS
        WITH release_mapping AS (
            SELECT
                tc.tag_name AS original_tag,
                COALESCE(
                    (SELECT tr2.tag_name
                     FROM tag_ranges tr2
                     WHERE tr2.tag_name NOT LIKE '%pre%'
                       AND tr2.tag_name NOT LIKE '%rc%'
                       AND tr2.tag_commit_date >= tr.tag_commit_date
                     ORDER BY tr2.tag_commit_date ASC
                     LIMIT 1),
                    tc.tag_name
                ) AS final_release
            FROM tag_commits tc
            LEFT JOIN tag_ranges tr ON tc.tag_name = tr.tag_name
            GROUP BY tc.tag_name, tr.tag_commit_date
        )
        SELECT
            rm.final_release AS tag_name,
            count(*) AS total_commits,
            count(*) FILTER (WHERE c.message LIKE '%Co-Authored-By: Claude%')
                AS claude_commits,
            round(
                count(*) FILTER (WHERE ai.security_tier = 'T1') * 1.0
              + count(*) FILTER (WHERE ai.security_tier = 'T2') * 0.4
            , 1) AS wt_sec
        FROM release_mapping rm
        JOIN tag_commits tc ON rm.original_tag = tc.tag_name
        INNER JOIN commits AS c ON tc.sha = c.sha
        INNER JOIN commits_with_ai AS ai ON c.sha = ai.sha
        WHERE rm.final_release NOT LIKE '%pre%'
          AND rm.final_release NOT LIKE '%rc%'
        GROUP BY rm.final_release
    """)

    # ── 4. release_bugs ──
    # Per-release bug counts from all three sources.
    con.execute("DROP VIEW IF EXISTS release_bugs")
    con.execute("""
        CREATE VIEW release_bugs AS
        SELECT
            attributed_release AS tag_name,
            count(*) AS bug_count,
            'github' AS source
        FROM bug_release_attribution
        GROUP BY attributed_release

        UNION ALL

        SELECT
            'v' || version AS tag_name,
            count(*) AS bug_count,
            'bugzilla' AS source
        FROM bugzilla_bugs
        WHERE version IS NOT NULL
          AND version != ''
          AND version != 'unspecified'
          AND resolution NOT IN ('DUPLICATE', 'INVALID', 'WONTFIX', 'WORKSFORME')
        GROUP BY version

        UNION ALL

        SELECT
            CASE
                WHEN version LIKE 'v%' THEN version
                ELSE 'v' || version
            END AS tag_name,
            count(*) AS bug_count,
            'ml' AS source
        FROM ml_bug_reports
        WHERE subject NOT LIKE 'DO NOT REPLY%'
        GROUP BY version
    """)

    # ── 5. release_table ──
    # THE core view. One row per release, five columns.
    # Includes Bugzilla-only releases (no commit data, but bug counts).
    con.execute("DROP VIEW IF EXISTS release_table")
    con.execute("""
        CREATE VIEW release_table AS

        -- Releases with git data
        SELECT
            rc.tag_name,
            COALESCE(b.total_bugs, 0) AS bug_count,
            rc.total_commits,
            rc.wt_sec,
            rc.claude_commits
        FROM release_commits AS rc
        LEFT JOIN (
            SELECT
                tag_name,
                sum(bug_count) AS total_bugs
            FROM release_bugs
            GROUP BY tag_name
        ) AS b ON rc.tag_name = b.tag_name

        UNION ALL

        -- Bugzilla-only releases (bugs but no git history)
        SELECT
            b.tag_name,
            b.total_bugs AS bug_count,
            0 AS total_commits,
            0.0 AS wt_sec,
            0 AS claude_commits
        FROM (
            SELECT
                tag_name,
                sum(bug_count) AS total_bugs
            FROM release_bugs
            GROUP BY tag_name
        ) AS b
        WHERE b.tag_name NOT IN (SELECT tag_name FROM release_commits)

        ORDER BY tag_name
    """)


def verify(con: duckdb.DuckDBPyConnection) -> None:
    """Print the release table for verification."""

    print("\n=== Full Release Table ===")
    print(f"  {'Release':10s} {'Bugs':>5s} {'Commits':>7s} {'WtSec':>6s} {'Claude':>6s}")
    print(f"  {'─'*10} {'─'*5} {'─'*7} {'─'*6} {'─'*6}")

    rows = con.execute("""
        SELECT tag_name, bug_count, total_commits, wt_sec, claude_commits
        FROM release_table
        ORDER BY tag_name
    """).fetchall()

    for r in rows:
        print(f"  {r[0]:10s} {r[1]:>5d} {r[2]:>7d} {r[3]:>6.1f} {r[4]:>6d}")

    total_bugs = sum(r[1] for r in rows)
    total_commits = sum(r[2] for r in rows)
    total_claude = sum(r[4] for r in rows)
    print(f"\n  Total: {len(rows)} releases, {total_bugs} bugs, "
          f"{total_commits} commits, {total_claude} Claude commits")


def main() -> None:
    print("=" * 60)
    print("Rebuilding analytical views in rsync_github.duckdb")
    print("=" * 60)
    con = duckdb.connect(str(DB_PATH))
    rebuild_views(con)
    print("All views rebuilt successfully.")
    verify(con)
    con.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
