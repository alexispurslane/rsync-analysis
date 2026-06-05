#!/usr/bin/env python3
"""
Fetch rsync mailing list archives and extract high-certainty regression
reports. These supplement Bugzilla data for pre-Bugzilla releases
(v2.4–v2.5.6) and overlap with Bugzilla for v2.5.7+.

STRICT CLASSIFICATION: Only posts where the author explicitly describes
behavior that CHANGED from a prior version are included. Mere bug reports
("rsync crashes") are NOT regressions — the author must indicate it used
to work, stopped working after an upgrade, etc.

Accepted signals (in original, non-quoted text):
  - "stopped working" / "quit working"
  - "no longer works" / "no longer <verb>"
  - "used to work" / "previously worked" / "worked fine before"
  - "since upgrading" / "after upgrading" / "after updating"
  - "regression" (explicit)
  - "broke" / "broken" + version reference ("in 2.5.4", "since 2.6.2")
  - "was working" + "now" / "but"

Rejected signals (too noisy):
  - "broken pipe" (just an error message)
  - "no longer" in release announcements
  - "regression test" (refers to test suite, not a user-facing regression)
  - Posts that start with "Re:" (replies, not original reports)
  - Quoted text (lines starting with > or |)
"""

import gzip
import io
import os
import re
import sys
import urllib.request
import duckdb

DB_PATH = os.path.join(os.path.dirname(__file__), "rsync_github.duckdb")

# Mailing list archive URLs (gzipped mbox format)
# Archives from https://lists.samba.org/archive/rsync/
ARCHIVE_BASE = "https://lists.samba.org/archive/rsync/"

# Pre-computed list of monthly archive filenames (2000-2024)
# Format: YYYY-Month.txt.gz
def get_archive_months():
    """Return list of monthly archive filenames to fetch."""
    months = []
    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    # rsync mailing list archives start from 2000
    for year in range(2000, 2025):
        for m_idx, m_name in enumerate(month_names, 1):
            months.append(f"{year}-{m_name}.txt.gz")
    return months


# ── Regression classification ──────────────────────────────────────────

# Strict: must indicate behavior CHANGED from a prior version
REGRESSION_PATTERNS = [
    # Explicit "it changed" signals
    (r'\bstopped\s+working\b', 'stopped_working'),
    (r'\bquit\s+working\b', 'stopped_working'),
    (r'\bno\s+longer\s+(?:works?|function|run|compile|sync|transfer|connect)\b', 'no_longer_works'),
    (r'\bused\s+to\s+(?:work|function|run|compile|sync)\b', 'used_to_work'),
    (r'\bpreviously\s+worked\b', 'used_to_work'),
    (r'\bworked\s+fine\s+before\b', 'used_to_work'),
    (r'\b(?:was|were)\s+working\b.*\b(?:now|but|broken|fail)\b', 'was_working_now'),
    (r'\bworked\s+fine\b.*\b(?:now|but|broken|fail)\b', 'was_working_now'),
    # Upgrade-attributed changes
    (r'\bsince\s+upgrad', 'since_upgrade'),
    (r'\bafter\s+upgrad', 'after_upgrade'),
    (r'\bafter\s+updating\b', 'after_update'),
    (r'\bsince\s+updating\b', 'since_update'),
    # Explicit regression
    (r'\bregression\b', 'explicit_regression'),
    # "broke/broken" + version reference
    (r'\bbroke(?:n)?\b.*\b(?:in|since|after|with)\s+v?\d+\.\d+', 'broke_in_version'),
]

# Noise patterns to EXCLUDE even if a regression pattern matches
NOISE_PATTERNS = [
    r'\bbroken\s+pipe\b',           # Error message, not a regression
    r'\bregression\s+test\b',       # Test suite reference
    r'\bno\s+longer\s+(?:need|want|require|support|include|update)\b',  # Not "doesn't work"
]

REGRESSION_COMPILED = [
    (re.compile(p, re.IGNORECASE), label) for p, label in REGRESSION_PATTERNS
]
NOISE_COMPILED = re.compile('|'.join(NOISE_PATTERNS), re.IGNORECASE)


def extract_version_from_subject(subj: str) -> str | None:
    """Extract rsync version number from subject line."""
    m = re.search(r'rsync[\s.-]*(\d+\.\d+(?:\.\d+)?)', subj, re.IGNORECASE)
    if m:
        v = m.group(1)
        parts = v.split('.')
        if int(parts[0]) <= 3 and int(parts[1]) <= 10:
            return v
    return None


def classify_post(subject: str, body: str) -> tuple[str | None, str | None]:
    """
    Classify a mailing list post as a regression report or not.
    
    Returns (version, signal_label) if it's a regression, (None, None) otherwise.
    Only considers original (non-quoted) text in the body.
    """
    # Skip replies
    if subject.lower().startswith('re:') or subject.lower().startswith('fwd:'):
        return None, None
    
    # Skip release announcements
    if re.search(r'\breleased\b', subject, re.IGNORECASE) and 'pre' in subject.lower():
        return None, None
    
    # Extract version from subject
    ver = extract_version_from_subject(subject)
    if not ver:
        return None, None
    
    # Build the text to scan: subject + non-quoted body lines
    lines_to_scan = [subject]
    for line in body.split('\n'):
        stripped = line.strip()
        # Skip quoted text
        if stripped.startswith('>') or stripped.startswith('|'):
            continue
        # Skip very short lines, URLs, signature markers
        if len(stripped) < 10:
            continue
        lines_to_scan.append(stripped)
    
    full_text = '\n'.join(lines_to_scan)
    
    # Check noise patterns first
    if NOISE_COMPILED.search(full_text):
        # If the ONLY match is a noise pattern, skip
        # But if there's also a genuine signal, keep it
        has_genuine = False
        for pattern, label in REGRESSION_COMPILED:
            # For "regression" pattern, also check it's not "regression test"
            if label == 'explicit_regression':
                for match in pattern.finditer(full_text):
                    context = full_text[max(0, match.start()-15):match.end()+15]
                    if 'regression test' not in context.lower():
                        has_genuine = True
                        break
            else:
                if pattern.search(full_text):
                    has_genuine = True
                    break
        if not has_genuine:
            return None, None
    
    # Check regression patterns
    for pattern, label in REGRESSION_COMPILED:
        if label == 'explicit_regression':
            # Special handling: must not be "regression test"
            for match in pattern.finditer(full_text):
                context = full_text[max(0, match.start()-15):match.end()+15]
                if 'regression test' not in context.lower():
                    return ver, label
        else:
            if pattern.search(full_text):
                return ver, label
    
    return None, None


def fetch_monthly_archive(filename: str) -> str | None:
    """Fetch and decompress a monthly mailing list archive."""
    url = f"{ARCHIVE_BASE}{filename}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rsync-research/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                raw = resp.read()
                try:
                    return gzip.decompress(raw).decode("utf-8", errors="replace")
                except gzip.BadGzipFile:
                    # Some archives might not be gzipped
                    return raw.decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        return None
    return None


def extract_posts_from_mbox(mbox_text: str) -> list[dict]:
    """Parse mbox text into individual posts with subject and body."""
    posts = []
    
    # Split on "From " lines (standard mbox separator)
    raw_messages = re.split(r'^From \S+', mbox_text, flags=re.MULTILINE)
    
    for msg in raw_messages[1:]:  # Skip empty first element
        # Extract subject
        sm = re.search(r'Subject:\s*(.+)', msg)
        if not sm:
            continue
        subject = sm.group(1).strip()
        
        # Extract date for context
        dm = re.search(r'Date:\s*(.+)', msg)
        date = dm.group(1).strip() if dm else ""
        
        # Body is everything after the first blank line
        body_parts = re.split(r'\n\n', msg, 1)
        body = body_parts[1] if len(body_parts) > 1 else ""
        
        posts.append({
            'subject': subject,
            'date': date,
            'body': body[:3000],  # Cap body length
        })
    
    return posts


def main():
    use_local = os.path.exists("/tmp/rsync-ml")
    
    regression_reports = []  # (version, subject, signal_label, date, evidence)
    
    if use_local:
        print("Using local mailing list archives from /tmp/rsync-ml/")
        import glob
        for f in sorted(glob.glob("/tmp/rsync-ml/*.txt")):
            with open(f, 'r', errors='replace') as fh:
                content = fh.read()
            
            posts = extract_posts_from_mbox(content)
            month = os.path.basename(f).replace('.txt', '')
            
            for post in posts:
                ver, label = classify_post(post['subject'], post['body'])
                if ver and label:
                    # Extract the evidentiary sentence
                    evidence = ""
                    for line in (post['subject'] + '\n' + post['body']).split('\n'):
                        stripped = line.strip()
                        if stripped.startswith('>') or stripped.startswith('|'):
                            continue
                        if any(p.search(stripped) for p, _ in REGRESSION_COMPILED if _ == label):
                            evidence = stripped[:200]
                            break
                    
                    regression_reports.append({
                        'version': ver,
                        'subject': post['subject'],
                        'signal': label,
                        'date': post['date'],
                        'evidence': evidence,
                    })
        
        print(f"  Found {len(regression_reports)} regression reports from local archives")
    
    else:
        print("Fetching mailing list archives from lists.samba.org...")
        months = get_archive_months()
        
        for i, filename in enumerate(months):
            if i % 12 == 0:
                print(f"  Processing year {filename[:4]}... ({i}/{len(months)})")
            
            mbox_text = fetch_monthly_archive(filename)
            if not mbox_text:
                continue
            
            posts = extract_posts_from_mbox(mbox_text)
            
            for post in posts:
                ver, label = classify_post(post['subject'], post['body'])
                if ver and label:
                    evidence = ""
                    for line in (post['subject'] + '\n' + post['body']).split('\n'):
                        stripped = line.strip()
                        if stripped.startswith('>') or stripped.startswith('|'):
                            continue
                        if any(p.search(stripped) for p, _ in REGRESSION_COMPILED if _ == label):
                            evidence = stripped[:200]
                            break
                    
                    regression_reports.append({
                        'version': ver,
                        'subject': post['subject'],
                        'signal': label,
                        'date': post['date'],
                        'evidence': evidence,
                    })
        
        print(f"  Found {len(regression_reports)} regression reports from remote archives")
    
    # ── Deduplicate ──
    # Same subject + same version = same report (cross-posted to multiple months)
    seen = set()
    unique_reports = []
    for r in regression_reports:
        key = (r['version'], r['subject'].lower().strip())
        if key not in seen:
            seen.add(key)
            unique_reports.append(r)
    
    print(f"  After dedup: {len(unique_reports)} unique regression reports")
    
    # ── Load into DuckDB ──
    con = duckdb.connect(DB_PATH)
    
    con.execute("DROP TABLE IF EXISTS ml_regressions")
    con.execute("""
        CREATE TABLE ml_regressions (
            version    VARCHAR,
            subject    VARCHAR,
            signal     VARCHAR,
            date       VARCHAR,
            evidence   VARCHAR
        )
    """)
    
    if unique_reports:
        con.executemany(
            "INSERT INTO ml_regressions VALUES (?, ?, ?, ?, ?)",
            [(r['version'], r['subject'], r['signal'], r['date'], r['evidence'])
             for r in unique_reports],
        )
    
    # Show summary by version
    print("\n  Regression reports by version:")
    ver_counts = con.execute("""
        SELECT version, count(*) as n,
               group_concat(DISTINCT signal) as signals
        FROM ml_regressions
        GROUP BY version
        ORDER BY version
    """).fetchall()
    
    in_bugzilla_era = False
    for ver, n, signals in ver_counts:
        # Mark which are pre-Bugzilla (v2.5.6 and earlier only have ML)
        if ver >= '2.5.7':
            in_bugzilla_era = True
        era = "(Bugzilla overlap)" if in_bugzilla_era else "(pre-Bugzilla)"
        print(f"    v{ver}: {n} reports {era}")
        print(f"      Signals: {signals}")
    
    con.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
