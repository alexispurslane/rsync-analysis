#!/usr/bin/env python3
"""
Fetch additions/deletions between consecutive tags via the GitHub compare API
and insert into tag_diff_stats table in the existing DuckDB database.
Safe to re-run — uses INSERT OR REPLACE.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_rsync_data import gh_api, paginate, DB_PATH, REPO, PER_PAGE

import duckdb


def main():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    con = duckdb.connect(str(DB_PATH))

    # Create table if not exists
    con.execute("""
        CREATE TABLE IF NOT EXISTS tag_diff_stats (
            tag_name           VARCHAR PRIMARY KEY,
            base_tag           VARCHAR,
            additions          INTEGER,
            deletions          INTEGER,
            changes           INTEGER,
            files_changed      INTEGER,
            commits_in_range   INTEGER
        )
    """)

    # Get ordered tags
    tags = paginate(f"repos/{REPO}/tags", {"per_page": PER_PAGE})
    tag_names = [t["name"] for t in tags if not t["name"].startswith("mbp")]
    tag_names.reverse()  # oldest first

    # Find which pairs we already have
    existing = set(r[0] for r in con.execute("SELECT tag_name FROM tag_diff_stats").fetchall())

    rows = []
    for i in range(1, len(tag_names)):
        head = tag_names[i]
        if head in existing:
            continue
        base = tag_names[i - 1]
        try:
            result = gh_api(f"repos/{REPO}/compare/{base}...{head}")
            files = result.get("files", [])
            additions = sum(f.get("additions", 0) for f in files)
            deletions = sum(f.get("deletions", 0) for f in files)
            rows.append({
                "tag_name": head,
                "base_tag": base,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
                "files_changed": len(files),
                "commits_in_range": result.get("total_commits", 0),
            })
            print(f"  {base} → {head}: +{additions}/-{deletions} ({len(files)} files)", flush=True)
        except Exception as e:
            print(f"  {base} → {head}: ERROR {e}", flush=True)

    if rows:
        for row in rows:
            con.execute("""
                INSERT OR REPLACE INTO tag_diff_stats
                (tag_name, base_tag, additions, deletions, changes, files_changed, commits_in_range)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [row["tag_name"], row["base_tag"], row["additions"],
                  row["deletions"], row["changes"], row["files_changed"],
                  row["commits_in_range"]])
        print(f"\nInserted {len(rows)} tag diff stats")
    else:
        print("All tag diffs already fetched!")

    # Quick verification
    count = con.execute("SELECT COUNT(*) FROM tag_diff_stats").fetchone()[0]
    print(f"  tag_diff_stats: {count} rows total")
    con.close()


if __name__ == "__main__":
    main()
