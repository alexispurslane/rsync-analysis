#!/usr/bin/env python3
"""
Download rsync mailing list archives from lists.samba.org.

Saves gzipped monthly archives and their decompressed .txt versions
to /tmp/rsync-ml/. The other pipeline scripts (fetch_mailinglist_bugs.py,
fetch_mailinglist_bugs.py) look for this directory and skip remote
fetching when it exists.

Archives: https://lists.samba.org/archive/rsync/
Format:   YYYY-Month.txt.gz
Range:    2000–2024
"""

import gzip
import os
import sys
import time
import urllib.request

ARCHIVE_BASE = "https://lists.samba.org/archive/rsync/"
DEST_DIR = "/tmp/rsync-ml"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def main() -> None:
    os.makedirs(DEST_DIR, exist_ok=True)

    fetched = 0
    skipped = 0
    failed = 0

    for year in range(2000, 2025):
        for month_name in MONTH_NAMES:
            filename = f"{year}-{month_name}.txt.gz"
            gz_path = os.path.join(DEST_DIR, filename)
            txt_path = os.path.join(DEST_DIR, f"{year}-{month_name}.txt")

            # Skip if decompressed file already exists
            if os.path.exists(txt_path):
                skipped += 1
                continue

            url = f"{ARCHIVE_BASE}{filename}"
            print(f"  Fetching {filename}...", end="", flush=True)

            try:
                req = urllib.request.Request(url, headers={"User-Agent": "rsync-research/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status != 200:
                        print(f" HTTP {resp.status}")
                        failed += 1
                        continue
                    raw = resp.read()

                # Save .gz
                with open(gz_path, "wb") as f:
                    f.write(raw)

                # Decompress to .txt
                try:
                    text = gzip.decompress(raw).decode("utf-8", errors="replace")
                    with open(txt_path, "w") as f:
                        f.write(text)
                    fetched += 1
                    print(" OK")
                except gzip.BadGzipFile:
                    # Some archives aren't actually gzipped
                    text = raw.decode("utf-8", errors="replace")
                    with open(txt_path, "w") as f:
                        f.write(text)
                    fetched += 1
                    print(" OK (not gzipped)")

            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                print(f" not found ({e})")
                failed += 1

            time.sleep(0.2)

    print(f"\nDone: {fetched} fetched, {skipped} already present, {failed} not found")


if __name__ == "__main__":
    main()
