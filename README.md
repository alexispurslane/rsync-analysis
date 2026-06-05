# rsync Bug-Rate Analysis

Reproduction pipeline for the rsync Claude bug-rate analysis. Fetches data from GitHub, Bugzilla, and the rsync mailing list, then generates an HTML report comparing Claude-assisted releases against the historical distribution.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (or pip)
- [`gh`](https://cli.github.com/) CLI, authenticated (`gh auth login`)
- The rsync mailing list archives extracted to `/tmp/rsync-ml/` (`.txt` files)
- (Optional) Bugzilla CSV at `/tmp/bugzilla_rsync_all.csv` — otherwise fetched live

## Setup

```bash
uv sync
```

## Run End-to-End

Scripts must run in order — each populates a shared DuckDB database that the next script reads from.

```bash
# 1. Fetch GitHub data (commits, PRs, issues, reviews, comments)
uv run python scripts/fetch_rsync_data.py

# 2. Fetch Bugzilla bugs
uv run python scripts/fetch_bugzilla_data.py

# 3. Extract regression reports from mailing list
uv run python scripts/fetch_mailinglist_data.py

# 4. Extract all bug reports from mailing list
uv run python scripts/fetch_mailinglist_bugs.py

# 5. Fetch tags/releases and compute commit ranges
uv run python scripts/enrich_releases.py

# 6. Build analytical views
uv run python scripts/build_views.py

# 7. Generate HTML report
uv run python scripts/regression_analysis.py
```

Output: `docs/index.html`

## Data

All data lives in `scripts/rsync_github.duckdb`. Delete it to start fresh.
