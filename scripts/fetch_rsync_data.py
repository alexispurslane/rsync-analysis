#!/usr/bin/env python3
"""
Fetch the commit and bug history of RsyncProject/rsync from GitHub
and load it into a DuckDB database for analysis.

Tables:
  - commits:  all commits on the default branch
  - bugs:     all bug reports and feature requests (not PRs; enhancements/questions kept for severity scoring)

Key alignment fields:
  - author_login / user_login: normalized GitHub username
  - created_at / closed_at: ISO8601 timestamps (TIMESTAMP)
  - labels: comma-separated label names (for filtering)
"""

import json
import subprocess
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


def fetch_commits() -> list[dict]:
    """Fetch all commits on the default branch."""
    print("Fetching commits...", flush=True)
    commits = paginate(f"repos/{REPO}/commits", {"per_page": PER_PAGE})
    print(f"  Got {len(commits)} commits", flush=True)

    rows = []
    for c in commits:
        commit = c.get("commit", {})
        author = commit.get("author", {})
        committer = commit.get("committer", {})
        author_login = (c.get("author") or {}).get("login")
        committer_login = (c.get("committer") or {}).get("login")

        rows.append({
            "sha": c["sha"],
            "author_login": author_login,
            "author_name": author.get("name"),
            "author_email": author.get("email"),
            "author_date": author.get("date"),
            "committer_login": committer_login,
            "committer_name": committer.get("name"),
            "committer_email": committer.get("email"),
            "committer_date": committer.get("date"),
            "message": commit.get("message", ""),
            "num_parents": len(c.get("parents", [])),
            "url": c.get("html_url", ""),
        })
    return rows


def fetch_bugs() -> list[dict]:
    """Fetch all bug reports and feature requests (excluding PRs).
    Enhancement and question issues are kept — they'll be scored severity=0
    by the LLM and filtered at analysis time, not storage time."""
    print("Fetching bugs...", flush=True)
    issues = paginate(f"repos/{REPO}/issues", {"state": "all", "per_page": PER_PAGE})
    # Filter out PRs (GitHub includes them in the issues endpoint)
    issues = [i for i in issues if "pull_request" not in i]
    print(f"  Got {len(issues)} issues (excluding PRs)", flush=True)

    rows = []
    for i in issues:
        user = i.get("user") or {}
        assignee = i.get("assignee") or {}
        assignees = i.get("assignees") or []
        closed_by = i.get("closed_by") or {}

        rows.append({
            "number": i["number"],
            "state": i["state"],
            "title": i.get("title", ""),
            "body": i.get("body") or "",
            "user_login": user.get("login"),
            "created_at": i.get("created_at"),
            "updated_at": i.get("updated_at"),
            "closed_at": i.get("closed_at"),
            "labels": ",".join(lb["name"] for lb in (i.get("labels") or [])),
            "milestone": (i.get("milestone") or {}).get("title") if i.get("milestone") else None,
            "assignee_login": assignee.get("login"),
            "assignee_logins": ",".join(a.get("login", "") for a in assignees),
            "closed_by_login": closed_by.get("login"),
            "num_comments": i.get("comments", 0),
            "url": i.get("html_url", ""),
        })
    return rows


def insert_into_duckdb(commits: list[dict], bugs: list[dict]):
    """Create tables and insert all data into DuckDB."""
    print(f"\nLoading data into DuckDB: {DB_PATH}", flush=True)

    # Remove existing DB to start fresh
    if DB_PATH.exists():
        DB_PATH.unlink()

    con = duckdb.connect(str(DB_PATH))

    con.execute("""
        CREATE TABLE commits (
            sha                VARCHAR PRIMARY KEY,
            author_login       VARCHAR,
            author_name        VARCHAR,
            author_email       VARCHAR,
            author_date        TIMESTAMP,
            committer_login    VARCHAR,
            committer_name     VARCHAR,
            committer_email    VARCHAR,
            committer_date     TIMESTAMP,
            message            VARCHAR,
            num_parents        INTEGER,
            url                VARCHAR
        )
    """)

    con.execute("""
        CREATE TABLE bugs (
            number             INTEGER PRIMARY KEY,
            state              VARCHAR,
            title              VARCHAR,
            body               VARCHAR,
            user_login         VARCHAR,
            created_at         TIMESTAMP,
            updated_at         TIMESTAMP,
            closed_at          TIMESTAMP,
            labels             VARCHAR,
            milestone          VARCHAR,
            assignee_login     VARCHAR,
            assignee_logins    VARCHAR,
            closed_by_login    VARCHAR,
            num_comments       INTEGER,
            url                VARCHAR
        )
    """)

    # Helper to insert rows
    def insert_rows(table: str, rows: list[dict]):
        if not rows:
            print(f"  No rows for {table}, skipping", flush=True)
            return
        cols = list(rows[0].keys())
        placeholders = ", ".join(["$" + str(i + 1) for i in range(len(cols))])
        col_names = ", ".join(cols)
        sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
        for row in rows:
            values = [row[c] for c in cols]
            con.execute(sql, values)
        print(f"  Inserted {len(rows)} rows into {table}", flush=True)

    insert_rows("commits", commits)
    insert_rows("bugs", bugs)

    # Create indices
    con.execute("CREATE INDEX idx_commits_author ON commits(author_login)")
    con.execute("CREATE INDEX idx_commits_committer ON commits(committer_login)")
    con.execute("CREATE INDEX idx_commits_date ON commits(author_date)")

    con.execute("CREATE INDEX idx_bugs_user ON bugs(user_login)")
    con.execute("CREATE INDEX idx_bugs_state ON bugs(state)")
    con.execute("CREATE INDEX idx_bugs_created ON bugs(created_at)")

    con.close()
    print("Done! Database saved.", flush=True)


def main():
    print("=" * 60)
    print(f"RsyncProject/rsync → DuckDB Data Pipeline")
    print("=" * 60)

    commits = fetch_commits()
    bugs = fetch_bugs()

    insert_into_duckdb(commits, bugs)

    # Quick verification
    con = duckdb.connect(str(DB_PATH), read_only=True)
    print("\n--- Verification ---")
    for table in ["commits", "bugs"]:
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")
    con.close()


if __name__ == "__main__":
    main()
