#!/usr/bin/env python3
"""
Fetch the entire commit, PR, and issue history of RsyncProject/rsync
from GitHub and load it into a DuckDB database for analysis.

Tables:
  - commits:      all commits on the default branch
  - pull_requests: all PRs (open + closed, including merged)
  - issues:        all issues (not PRs)
  - pr_commits:    linking table - which commits belong to which PRs
  - pr_reviews:    PR review data
  - issue_comments: comments on issues
  - pr_comments:   review comments on PRs

Key alignment fields:
  - author_login / user_login: normalized GitHub username
  - created_at / merged_at / closed_at: ISO8601 timestamps (TIMESTAMP)
  - labels: comma-separated label names (for filtering)
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
    # Build query string from params
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
        # Small sleep to be nice to the API
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


def fetch_pull_requests() -> list[dict]:
    """Fetch all pull requests (open + closed)."""
    print("Fetching pull requests...", flush=True)
    prs = paginate(f"repos/{REPO}/pulls", {"state": "all", "per_page": PER_PAGE})
    print(f"  Got {len(prs)} PRs", flush=True)

    rows = []
    for p in prs:
        user = p.get("user") or {}
        merged_by = p.get("merged_by") or {}
        base = p.get("base", {})
        head = p.get("head", {})

        rows.append({
            "number": p["number"],
            "state": p["state"],
            "title": p.get("title", ""),
            "body": p.get("body") or "",
            "user_login": user.get("login"),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
            "closed_at": p.get("closed_at"),
            "merged_at": p.get("merged_at"),
            "merged_by_login": merged_by.get("login"),
            "merge_commit_sha": p.get("merge_commit_sha"),
            "base_ref": base.get("ref"),
            "head_ref": head.get("ref"),
            "head_repo": (head.get("repo") or {}).get("full_name"),
            "labels": ",".join(lb["name"] for lb in (p.get("labels") or [])),
            "milestone": (p.get("milestone") or {}).get("title") if p.get("milestone") else None,
            "draft": p.get("draft", False),
            "commits_count": p.get("commits"),
            "additions": p.get("additions"),
            "deletions": p.get("deletions"),
            "changed_files": p.get("changed_files"),
            "url": p.get("html_url", ""),
        })
    return rows


def fetch_issues() -> list[dict]:
    """Fetch all issues (excluding PRs)."""
    print("Fetching issues...", flush=True)
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


def fetch_pr_commits(prs: list[dict]) -> list[dict]:
    """For each PR, fetch the commits it contains (the linking table)."""
    print(f"Fetching PR↔commit links for {len(prs)} PRs...", flush=True)
    rows = []
    for idx, pr in enumerate(prs):
        pr_number = pr["number"]
        if (idx + 1) % 50 == 0:
            print(f"  Processing PR #{pr_number} ({idx+1}/{len(prs)})...", flush=True)
        try:
            commits = paginate(f"repos/{REPO}/pulls/{pr_number}/commits")
            for c in commits:
                rows.append({
                    "pr_number": pr_number,
                    "sha": c["sha"],
                })
        except subprocess.CalledProcessError as e:
            # Some PRs may not have commits accessible
            print(f"  Warning: could not fetch commits for PR #{pr_number}: {e.stderr[:120]}", flush=True)
            continue
        time.sleep(0.2)

    print(f"  Got {len(rows)} PR↔commit links", flush=True)
    return rows


def fetch_pr_reviews(prs: list[dict]) -> list[dict]:
    """Fetch reviews for all PRs."""
    print(f"Fetching PR reviews for {len(prs)} PRs...", flush=True)
    rows = []
    for idx, pr in enumerate(prs):
        pr_number = pr["number"]
        if (idx + 1) % 50 == 0:
            print(f"  Processing reviews for PR #{pr_number} ({idx+1}/{len(prs)})...", flush=True)
        try:
            reviews = paginate(f"repos/{REPO}/pulls/{pr_number}/reviews")
            for r in reviews:
                user = r.get("user") or {}
                rows.append({
                    "pr_number": pr_number,
                    "review_id": r["id"],
                    "user_login": user.get("login"),
                    "state": r.get("state"),
                    "body": r.get("body") or "",
                    "submitted_at": r.get("submitted_at"),
                    "commit_id": r.get("commit_id"),
                })
        except subprocess.CalledProcessError:
            continue
        time.sleep(0.2)

    print(f"  Got {len(rows)} PR reviews", flush=True)
    return rows


def fetch_issue_comments() -> list[dict]:
    """Fetch all issue comments (includes comments on PRs too)."""
    print("Fetching issue comments...", flush=True)
    comments = paginate(f"repos/{REPO}/issues/comments", {"per_page": PER_PAGE})
    print(f"  Got {len(comments)} issue comments", flush=True)

    rows = []
    for c in comments:
        user = c.get("user") or {}
        # Extract issue/PR number from URL
        # URL format: .../issues/123/comments/... or .../pull/123/comments/...
        url = c.get("html_url", "")
        parts = url.split("/")
        issue_number = None
        for i, part in enumerate(parts):
            if part in ("issues", "pull") and i + 1 < len(parts):
                try:
                    issue_number = int(parts[i + 1])
                except ValueError:
                    pass
                break

        rows.append({
            "comment_id": c["id"],
            "issue_number": issue_number,
            "user_login": user.get("login"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
            "body": c.get("body") or "",
            "url": url,
        })
    return rows


def fetch_pr_review_comments() -> list[dict]:
    """Fetch PR review comments (inline code comments)."""
    print("Fetching PR review comments...", flush=True)
    comments = paginate(f"repos/{REPO}/pulls/comments", {"per_page": PER_PAGE})
    print(f"  Got {len(comments)} PR review comments", flush=True)

    rows = []
    for c in comments:
        user = c.get("user") or {}
        rows.append({
            "comment_id": c["id"],
            "pr_number": c.get("pull_request_id"),  # This is actually the PR's review comment ID
            "user_login": user.get("login"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
            "body": c.get("body") or "",
            "path": c.get("path"),
            "line": c.get("line"),
            "diff_hunk": c.get("diff_hunk", ""),
            "commit_id": c.get("commit_id"),
            "url": c.get("html_url", ""),
        })
    return rows


def insert_into_duckdb(
    commits: list[dict],
    pull_requests: list[dict],
    issues: list[dict],
    pr_commits: list[dict],
    pr_reviews: list[dict],
    issue_comments: list[dict],
    pr_review_comments: list[dict],
):
    """Create tables and insert all data into DuckDB."""
    print(f"\nLoading data into DuckDB: {DB_PATH}", flush=True)
    db_path_str = str(DB_PATH)

    # Remove existing DB to start fresh
    if DB_PATH.exists():
        DB_PATH.unlink()

    con = duckdb.connect(db_path_str)

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
        CREATE TABLE pull_requests (
            number             INTEGER PRIMARY KEY,
            state              VARCHAR,
            title              VARCHAR,
            body               VARCHAR,
            user_login         VARCHAR,
            created_at         TIMESTAMP,
            updated_at         TIMESTAMP,
            closed_at          TIMESTAMP,
            merged_at          TIMESTAMP,
            merged_by_login    VARCHAR,
            merge_commit_sha   VARCHAR,
            base_ref           VARCHAR,
            head_ref           VARCHAR,
            head_repo          VARCHAR,
            labels             VARCHAR,
            milestone          VARCHAR,
            draft              BOOLEAN,
            commits_count      INTEGER,
            additions          INTEGER,
            deletions          INTEGER,
            changed_files      INTEGER,
            url                VARCHAR
        )
    """)

    con.execute("""
        CREATE TABLE issues (
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

    con.execute("""
        CREATE TABLE pr_commits (
            pr_number          INTEGER,
            sha                VARCHAR,
            PRIMARY KEY (pr_number, sha)
        )
    """)

    con.execute("""
        CREATE TABLE pr_reviews (
            pr_number          INTEGER,
            review_id          BIGINT PRIMARY KEY,
            user_login         VARCHAR,
            state              VARCHAR,
            body               VARCHAR,
            submitted_at       TIMESTAMP,
            commit_id          VARCHAR
        )
    """)

    con.execute("""
        CREATE TABLE issue_comments (
            comment_id         BIGINT PRIMARY KEY,
            issue_number       INTEGER,
            user_login         VARCHAR,
            created_at         TIMESTAMP,
            updated_at         TIMESTAMP,
            body               VARCHAR,
            url                VARCHAR
        )
    """)

    con.execute("""
        CREATE TABLE pr_review_comments (
            comment_id         BIGINT PRIMARY KEY,
            pr_number          INTEGER,
            user_login         VARCHAR,
            created_at         TIMESTAMP,
            updated_at         TIMESTAMP,
            body               VARCHAR,
            path               VARCHAR,
            line               INTEGER,
            diff_hunk          VARCHAR,
            commit_id          VARCHAR,
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
            # Convert None to None (Python None maps to SQL NULL)
            con.execute(sql, values)
        print(f"  Inserted {len(rows)} rows into {table}", flush=True)

    insert_rows("commits", commits)
    insert_rows("pull_requests", pull_requests)
    insert_rows("issues", issues)
    insert_rows("pr_commits", pr_commits)
    insert_rows("pr_reviews", pr_reviews)
    insert_rows("issue_comments", issue_comments)
    insert_rows("pr_review_comments", pr_review_comments)

    # Create indices for fast cross-queries
    con.execute("CREATE INDEX idx_commits_author ON commits(author_login)")
    con.execute("CREATE INDEX idx_commits_committer ON commits(committer_login)")
    con.execute("CREATE INDEX idx_commits_date ON commits(author_date)")

    con.execute("CREATE INDEX idx_prs_user ON pull_requests(user_login)")
    con.execute("CREATE INDEX idx_prs_state ON pull_requests(state)")
    con.execute("CREATE INDEX idx_prs_created ON pull_requests(created_at)")
    con.execute("CREATE INDEX idx_prs_merged ON pull_requests(merged_at)")

    con.execute("CREATE INDEX idx_issues_user ON issues(user_login)")
    con.execute("CREATE INDEX idx_issues_state ON issues(state)")
    con.execute("CREATE INDEX idx_issues_created ON issues(created_at)")

    con.execute("CREATE INDEX idx_pr_commits_pr ON pr_commits(pr_number)")
    con.execute("CREATE INDEX idx_pr_commits_sha ON pr_commits(sha)")

    con.execute("CREATE INDEX idx_pr_reviews_pr ON pr_reviews(pr_number)")
    con.execute("CREATE INDEX idx_issue_comments_issue ON issue_comments(issue_number)")
    con.execute("CREATE INDEX idx_pr_review_comments_pr ON pr_review_comments(pr_number)")

    con.close()
    print("Done! Database saved.", flush=True)


def main():
    print("=" * 60)
    print(f"RsyncProject/rsync → DuckDB Data Pipeline")
    print("=" * 60)

    # Fetch all data
    commits = fetch_commits()
    pull_requests = fetch_pull_requests()
    issues = fetch_issues()

    # These depend on PR list
    pr_commits = fetch_pr_commits(pull_requests)
    pr_reviews = fetch_pr_reviews(pull_requests)

    # Standalone
    issue_comments = fetch_issue_comments()
    pr_review_comments = fetch_pr_review_comments()

    # Load into DuckDB
    insert_into_duckdb(
        commits, pull_requests, issues,
        pr_commits, pr_reviews, issue_comments, pr_review_comments,
    )

    # Quick verification
    con = duckdb.connect(str(DB_PATH), read_only=True)
    print("\n--- Verification ---")
    for table in ["commits", "pull_requests", "issues", "pr_commits", "pr_reviews", "issue_comments", "pr_review_comments"]:
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")

    print("\nSample cross-query: PRs with their commits")
    result = con.execute("""
        SELECT pr.number, pr.title, pr.state, COUNT(pc.sha) as commit_count
        FROM pull_requests pr
        LEFT JOIN pr_commits pc ON pr.number = pc.pr_number
        GROUP BY pr.number, pr.title, pr.state
        ORDER BY commit_count DESC
        LIMIT 5
    """).fetchall()
    for row in result:
        print(f"  PR #{row[0]}: {row[1][:50]}... [{row[2]}] — {row[3]} commits")

    con.close()


if __name__ == "__main__":
    main()
