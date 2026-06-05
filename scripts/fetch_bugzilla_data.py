#!/usr/bin/env python3
"""
Fetch rsync Bugzilla bug data and load into the DuckDB database.

Bugzilla is rsync's pre-GitHub issue tracker (v2.5.7–v3.2.0 era).
Uses the CSV export from bugzilla.samba.org with fields:
  bug_id, short_desc, changeddate, resolution, bug_status, component,
  bug_severity, version, op_sys

The `version` field maps cleanly to rsync release versions, giving us
per-release regression counts for 25 versions the GitHub data doesn't cover.
"""

import csv
import io
import os
import sys
import urllib.request
import duckdb

DB_PATH = os.path.join(os.path.dirname(__file__), "rsync_github.duckdb")
BUGZILLA_CSV_URL = "https://bugzilla.samba.org/buglist.cgi?ctype=csv&product=rsync&columnlist=bug_id%2Cshort_desc%2Cchangeddate%2Cresolution%2Cbug_status%2Ccomponent%2Cbug_severity%2Cversion%2Cop_sys"

# The Bugzilla CSV export paginates at ~10k results. We'll fetch all pages.
BUGZILLA_CSV_BASE = "https://bugzilla.samba.org/buglist.cgi"
QUERY_PARAMS = {
    "ctype": "csv",
    "product": "rsync",
    "columnlist": "bug_id,short_desc,changeddate,resolution,bug_status,component,bug_severity,version,op_sys",
}


def fetch_all_bugs() -> list[dict]:
    """Fetch all rsync bugs from Bugzilla CSV export."""
    # Build URL
    param_str = "&".join(f"{k}={v}" for k, v in QUERY_PARAMS.items())
    url = f"{BUGZILLA_CSV_BASE}?{param_str}"
    
    print(f"Fetching Bugzilla CSV from {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "rsync-research/1.0"})
    
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    
    reader = csv.DictReader(io.StringIO(raw))
    bugs = list(reader)
    print(f"  Fetched {len(bugs)} bugs")
    return bugs


def load_into_duckdb(bugs: list[dict], db_path: str = DB_PATH):
    """Load Bugzilla bugs into duckdb as a `bugzilla_bugs` table."""
    con = duckdb.connect(db_path)
    
    con.execute("DROP TABLE IF EXISTS bugzilla_bugs")
    con.execute("""
        CREATE TABLE bugzilla_bugs (
            bug_id      INTEGER PRIMARY KEY,
            short_desc  VARCHAR,
            changeddate VARCHAR,
            resolution  VARCHAR,
            bug_status  VARCHAR,
            component   VARCHAR,
            bug_severity VARCHAR,
            version     VARCHAR,
            op_sys      VARCHAR
        )
    """)
    
    rows = []
    for b in bugs:
        try:
            rows.append((
                int(b["bug_id"]),
                b.get("short_desc", ""),
                b.get("changeddate", ""),
                b.get("resolution", "").strip(),
                b.get("bug_status", ""),
                b.get("component", ""),
                b.get("bug_severity", ""),
                b.get("version", ""),
                b.get("op_sys", ""),
            ))
        except (ValueError, KeyError) as e:
            print(f"  Skipping malformed bug: {b.get('bug_id', '?')} ({e})")
            continue
    
    con.executemany(
        "INSERT INTO bugzilla_bugs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    
    count = con.execute("SELECT count(*) FROM bugzilla_bugs").fetchone()[0]
    print(f"  Loaded {count} bugs into bugzilla_bugs table")
    
    # Show version coverage
    ver_count = con.execute("""
        SELECT version, count(*) as n
        FROM bugzilla_bugs
        WHERE version != 'unspecified' AND version != ''
        GROUP BY version
        ORDER BY version
    """).fetchall()
    print(f"  Versions with bugs: {len(ver_count)}")
    for v, n in ver_count:
        print(f"    v{v}: {n} bugs")
    
    con.close()


def main():
    # Allow using a pre-downloaded CSV for offline development
    csv_path = "/tmp/bugzilla_rsync_all.csv"
    if os.path.exists(csv_path):
        print(f"Using pre-downloaded CSV from {csv_path}")
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            bugs = list(reader)
        print(f"  Loaded {len(bugs)} bugs from local CSV")
    else:
        bugs = fetch_all_bugs()
    
    # Clean up resolution field (Bugzilla exports " ---" for unresolved)
    for b in bugs:
        if b.get("resolution"):
            b["resolution"] = b["resolution"].strip()
    
    load_into_duckdb(bugs)


if __name__ == "__main__":
    main()
