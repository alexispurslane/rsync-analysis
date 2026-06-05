#!/usr/bin/env python3
"""
Enrich the rsync_github.duckdb with tags/releases data and connect them
to the range of commits they encompass and the PRs they included.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import duckdb

REPO = "RsyncProject/rsync"
DB_PATH = Path(__file__).parent / "rsync_github.duckdb"
PER_PAGE = 100


def gh_api(endpoint: str, params: dict | None = None) -> dict | list:
    """Call GitHub API via `gh api` and return parsed JSON."""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        endpoint = f"{endpoint}?{qs}"
    cmd = ["gh", "api", endpoint]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def paginate(endpoint: str, params: dict | None = None) -> list:
    """Paginate through all pages of a GitHub API endpoint."""
    all_items = []
    page = 1
    params = dict(params or {})
    params["per_page"] = PER_PAGE

    while True:
        params["page"] = page
        print(f"  Fetching page {page} of {endpoint}...", flush=True)
        items = gh_api(endpoint, params)
        if not items:
            break
        all_items.extend(items)
        if len(items) < PER_PAGE:
            break
        page += 1
        time.sleep(0.3)

    return all_items


def fetch_tags() -> list[dict]:
    """Fetch all tags with the commit SHA each points to."""
    print("Fetching tags...", flush=True)
    tags = paginate(f"repos/{REPO}/tags")
    print(f"  Got {len(tags)} tags", flush=True)

    rows = []
    for t in tags:
        commit = t.get("commit", {})
        rows.append({
            "name": t["name"],
            "target_sha": commit.get("sha"),
            "url": f"https://github.com/{REPO}/releases/tag/{t['name']}",
        })
    return rows


def fetch_releases() -> list[dict]:
    """Fetch all GitHub releases (rich metadata)."""
    print("Fetching releases...", flush=True)
    releases = paginate(f"repos/{REPO}/releases")
    print(f"  Got {len(releases)} releases", flush=True)

    rows = []
    for r in releases:
        author = r.get("author") or {}
        rows.append({
            "release_id": r["id"],
            "tag_name": r.get("tag_name"),
            "name": r.get("name"),
            "body": r.get("body") or "",
            "draft": r.get("draft", False),
            "prerelease": r.get("prerelease", False),
            "created_at": r.get("created_at"),
            "published_at": r.get("published_at"),
            "author_login": author.get("login"),
            "target_commitish": r.get("target_commitish"),
            "url": r.get("html_url"),
        })
    return rows


def compute_version_order(tag_names: list[str]) -> list[str]:
    """
    Sort tags in version order (ascending).
    We'll use the commit date order from the DB instead for reliability,
    but return them in semver-like order for range computation.
    """
    # We'll sort by date via the DB - this is just a placeholder
    return tag_names


def main():
    print("=" * 60)
    print("RsyncProject/rsync → Tags & Releases Enrichment")
    print("=" * 60)

    # Fetch data
    tags = fetch_tags()
    releases = fetch_releases()

    # Load into DuckDB
    print(f"\nLoading data into DuckDB: {DB_PATH}", flush=True)
    con = duckdb.connect(str(DB_PATH))

    # ── Tags table ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            name          VARCHAR PRIMARY KEY,
            target_sha    VARCHAR,
            url           VARCHAR
        )
    """)
    con.execute("DELETE FROM tags")
    for t in tags:
        con.execute(
            "INSERT INTO tags (name, target_sha, url) VALUES ($1, $2, $3)",
            [t["name"], t["target_sha"], t["url"]],
        )
    print(f"  Inserted {len(tags)} rows into tags", flush=True)

    # ── Releases table ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS releases (
            release_id        BIGINT PRIMARY KEY,
            tag_name          VARCHAR,
            name              VARCHAR,
            body              VARCHAR,
            draft             BOOLEAN,
            prerelease        BOOLEAN,
            created_at        TIMESTAMP,
            published_at      TIMESTAMP,
            author_login      VARCHAR,
            target_commitish  VARCHAR,
            url               VARCHAR
        )
    """)
    con.execute("DELETE FROM releases")
    for r in releases:
        con.execute(
            """INSERT INTO releases (release_id, tag_name, name, body, draft, prerelease,
               created_at, published_at, author_login, target_commitish, url)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
            [r["release_id"], r["tag_name"], r["name"], r["body"], r["draft"],
             r["prerelease"], r["created_at"], r["published_at"], r["author_login"],
             r["target_commitish"], r["url"]],
        )
    print(f"  Inserted {len(releases)} rows into releases", flush=True)

    # ── Index tags on target_sha ──
    try:
        con.execute("CREATE INDEX idx_tags_sha ON tags(target_sha)")
    except Exception:
        pass  # May already exist from previous run

    # ── Compute tag→commit ranges ──
    # Strategy: Sort tags by the committer_date of their target_sha in the commits table.
    # For each consecutive pair of tags, the "release range" is the commits between
    # the prior tag's commit and this tag's commit (exclusive start, inclusive end).
    # We use the commit ordering on master (by committer_date) to determine ranges.

    print("\nComputing tag→commit ranges...", flush=True)

    # First, assign a sequential row number to all commits ordered by committer_date
    # This gives us a total ordering on the master branch
    con.execute("DROP SEQUENCE IF EXISTS commit_ord_seq")
    con.execute("CREATE SEQUENCE commit_ord_seq")

    # Build a real table with commit ordering (needs to survive connection close)
    con.execute("DROP TABLE IF EXISTS commit_order")
    con.execute("""
        CREATE TABLE commit_order AS
        SELECT sha, committer_date,
               ROW_NUMBER() OVER (ORDER BY committer_date, sha) as commit_ordinal
        FROM commits
        WHERE committer_date IS NOT NULL
    """)

    # Build tag ordering: tag name + the ordinal of the commit they point to
    con.execute("DROP TABLE IF EXISTS tag_order")
    con.execute("""
        CREATE TABLE tag_order AS
        SELECT 
            t.name as tag_name,
            t.target_sha,
            co.commit_ordinal,
            co.committer_date as tag_commit_date
        FROM tags t
        JOIN commit_order co ON t.target_sha = co.sha
        WHERE t.target_sha IS NOT NULL
        ORDER BY co.commit_ordinal
    """)

    tag_count = con.execute("SELECT COUNT(*) FROM tag_order").fetchone()[0]
    print(f"  {tag_count} tags matched to commits in the commit history", flush=True)

    # Now for each tag, find the previous tag to compute the range
    # prev_tag = the tag immediately before this one in commit_ordinal order
    con.execute("DROP TABLE IF EXISTS tag_ranges")
    con.execute("""
        CREATE TABLE tag_ranges AS
        SELECT 
            curr.tag_name,
            curr.target_sha as tag_sha,
            curr.commit_ordinal as tag_ordinal,
            curr.tag_commit_date,
            prev.tag_name as prev_tag_name,
            prev.target_sha as prev_tag_sha,
            prev.commit_ordinal as prev_tag_ordinal,
            prev.tag_commit_date as prev_tag_date
        FROM tag_order curr
        LEFT JOIN tag_order prev ON curr.commit_ordinal > prev.commit_ordinal
        QUALIFY ROW_NUMBER() OVER (PARTITION BY curr.tag_name ORDER BY prev.commit_ordinal DESC) = 1
    """)

    # Create the tag_commits linking table: which commits are in each tag's range
    # Commits where prev_tag_ordinal < commit_ordinal <= tag_ordinal
    con.execute("""
        CREATE TABLE IF NOT EXISTS tag_commits (
            tag_name      VARCHAR,
            sha           VARCHAR,
            is_tag_commit BOOLEAN,  -- true if this is the exact commit the tag points to
            PRIMARY KEY (tag_name, sha)
        )
    """)
    con.execute("DELETE FROM tag_commits")

    # Insert the range of commits for each tag
    con.execute("""
        INSERT INTO tag_commits (tag_name, sha, is_tag_commit)
        SELECT 
            tr.tag_name,
            co.sha,
            CASE WHEN co.sha = tr.tag_sha THEN true ELSE false END as is_tag_commit
        FROM tag_ranges tr
        JOIN commit_order co ON co.commit_ordinal > tr.prev_tag_ordinal
                            AND co.commit_ordinal <= tr.tag_ordinal
        WHERE tr.prev_tag_ordinal IS NOT NULL
    """)

    # For the very first tag (no prev), include all commits up to it
    con.execute("""
        INSERT INTO tag_commits (tag_name, sha, is_tag_commit)
        SELECT 
            tr.tag_name,
            co.sha,
            CASE WHEN co.sha = tr.tag_sha THEN true ELSE false END as is_tag_commit
        FROM tag_ranges tr
        JOIN commit_order co ON co.commit_ordinal <= tr.tag_ordinal
        WHERE tr.prev_tag_ordinal IS NULL
    """)

    tc_count = con.execute("SELECT COUNT(*) FROM tag_commits").fetchone()[0]
    print(f"  Inserted {tc_count:,} rows into tag_commits (tag↔commit links)", flush=True)

    # ── Create indices ──
    try:
        con.execute("CREATE INDEX idx_tag_commits_tag ON tag_commits(tag_name)")
        con.execute("CREATE INDEX idx_tag_commits_sha ON tag_commits(sha)")
        con.execute("CREATE INDEX idx_releases_tag ON releases(tag_name)")
    except Exception:
        pass

    con.close()

    # ── Verification ──
    print("\n--- Verification ---", flush=True)
    con = duckdb.connect(str(DB_PATH))

    print("\nTags with commit counts:")
    for row in con.execute("""
        SELECT tr.tag_name, tr.prev_tag_name, tr.tag_commit_date,
               (SELECT COUNT(*) FROM tag_commits tc WHERE tc.tag_name = tr.tag_name) as commit_count
        FROM tag_ranges tr
        ORDER BY tr.tag_commit_date DESC
        LIMIT 15
    """).fetchall():
        prev = f"({row[1]})" if row[1] else "(initial)"
        print(f"  {row[0]:20s} {prev:20s} {row[3]:>5} commits  {str(row[2])[:10]}")

    con.close()
    print("\nDone!", flush=True)


if __name__ == "__main__":
    main()
