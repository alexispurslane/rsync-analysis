#!/usr/bin/env python3
"""
Extract bug reports from rsync mailing list archives.

Unlike the old fetch_mailinglist_data.py which only looked for regression
signals ("broke after upgrading"), this extracts ALL posts that look like
bug reports — any post mentioning a version number AND a bug signal word.

Excludes:
  - DO NOT REPLY posts (Bugzilla mirrors, already counted via bugzilla_bugs)
  - Replies (Subject: Re:) — only counts original reports
  - Version strings that don't map to real rsync releases

Normalizes version numbers:
  - 2.6 → 2.6.0
  - 3.0 → 3.0.0
  - 3.00 → 3.0.0
  - 3.1 → 3.1.0
  - 3.10 → ambiguous, skip
  - 4.5.4 → not a real rsync version, skip

Deduplicates: same subject line in same archive file = 1 report.
"""

import os
import re
from collections import Counter
from pathlib import Path

import duckdb

ML_DIR = Path("/tmp/rsync-ml")
DB_PATH = Path(__file__).resolve().parent.parent.parent / "rsync_github.duckdb"

# ── Version matching ──
VERSION_RE = re.compile(r'rsync[- ](\d+\.\d+(?:\.\d+)?)', re.I)

# Valid rsync major.minor pairs
VALID_MAJOR_MINOR = {
    (1, 6), (1, 7),
    (2, 0), (2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6),
    (3, 0), (3, 1), (3, 2), (3, 3), (3, 4),
}

# Bug signal words in subject line
BUG_SIGNALS = re.compile(
    r'\b(bug|error|fail|crash|broken|broke|hang|stuck|segfault|seg fault|'
    r'problem|issue|wrong|corrupt|doesn\'t|does not|can\'t|cannot|'
    r'coredump|core dump|panic|deadlock|overflow|oob|'
    r'memory|leak|slow|timeout|time out|'
    r'r\s*sync\s*error|protocol\s*error|'
    r'incompatible|unexpected|invalid|denied|permission)\b',
    re.I
)


def normalize_version(raw: str) -> str | None:
    """Normalize a version string to X.Y.Z, or return None if invalid."""
    parts = raw.split('.')
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except (ValueError, IndexError):
        return None

    # Reject unknown major.minor
    if (major, minor) not in VALID_MAJOR_MINOR:
        return None

    # Get patch version
    if len(parts) >= 3:
        try:
            patch = int(parts[2])
        except ValueError:
            patch = 0
    else:
        patch = 0

    # Special: treat 3.00 as 3.0.0
    if major == 3 and minor == 0 and len(parts) >= 3:
        try:
            p = int(parts[2])
            if p == 0:
                patch = 0
        except ValueError:
            pass

    return f"{major}.{minor}.{patch}"


def extract_reports(archive_dir: Path) -> list[dict]:
    """Extract all bug reports from ML archives."""
    files = sorted(
        f for f in archive_dir.iterdir()
        if f.suffix == '.txt' and not f.name.endswith('.txt.gz')
    )

    reports = []
    seen = set()  # (version, normalized_subject) for dedup within file

    for fpath in files:
        with open(fpath, errors='replace') as f:
            content = f.read()

        seen.clear()

        for line in content.split('\n'):
            if not line.startswith('Subject:'):
                continue

            subject = line[len('Subject: '):]

            # Skip replies and Bugzilla mirrors
            if subject.startswith('Re:'):
                continue
            if subject.startswith('DO NOT REPLY'):
                continue
            if subject.startswith('[rsync-announce]'):
                continue

            # Must have version reference
            ver_match = VERSION_RE.search(subject)
            if not ver_match:
                continue

            # Must have bug signal word
            if not BUG_SIGNALS.search(subject):
                continue

            raw_version = ver_match.group(1)
            version = normalize_version(raw_version)
            if version is None:
                continue

            # Normalize subject for dedup (lowercase, strip extra spaces)
            norm_subject = ' '.join(subject.lower().split())

            # Dedup: same version + same subject in same file
            key = (version, norm_subject)
            if key in seen:
                continue
            seen.add(key)

            # Derive year-month from filename
            year_month = fpath.stem  # e.g. "2006-January"

            reports.append({
                'version': version,
                'subject': subject.strip(),
                'archive_file': fpath.name,
                'year_month': year_month,
            })

    return reports


def main() -> None:
    print(f"Reading ML archives from {ML_DIR}...")
    reports = extract_reports(ML_DIR)
    print(f"  Found {len(reports)} bug reports")

    # Show stats
    ver_counts = Counter(r['version'] for r in reports)
    print(f"\n  By version:")
    for v, n in sorted(ver_counts.items()):
        print(f"    v{v:10s} {n}")

    # Store in DB
    print(f"\nStoring in {DB_PATH}...")
    con = duckdb.connect(str(DB_PATH))

    # Drop old table and recreate
    con.execute("DROP TABLE IF EXISTS ml_bug_reports")
    con.execute("""
        CREATE TABLE ml_bug_reports (
            version VARCHAR,
            subject VARCHAR,
            archive_file VARCHAR,
            year_month VARCHAR
        )
    """)

    if reports:
        con.executemany(
            "INSERT INTO ml_bug_reports VALUES (?, ?, ?, ?)",
            [(r['version'], r['subject'], r['archive_file'], r['year_month']) for r in reports]
        )

    count = con.execute("SELECT count(*) FROM ml_bug_reports").fetchone()[0]
    print(f"  Stored {count} rows in ml_bug_reports")

    # Quick validation
    print(f"\n  Sample rows:")
    for r in con.execute("SELECT version, subject FROM ml_bug_reports ORDER BY version LIMIT 15").fetchall():
        print(f"    v{r[0]:10s} {r[1][:70]}")

    con.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
