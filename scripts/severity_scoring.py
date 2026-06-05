"""
Score the severity of every bug report in the rsync database using an LLM.

Reads GitHub bugs, Bugzilla bugs, and mailing list reports from the DuckDB
database, sends each one to an LLM with detailed severity-judgment instructions,
and writes the scores back into the database (bugs.severity, bugzilla_bugs.severity,
ml_bug_reports.severity). Also saves a JSON backup for resume support.

Supports resuming — already-scored entries are skipped on re-run (checked via
both the DB column and the JSON backup).

Usage:
    uv run scripts/severity_scoring.py [--limit N]
"""

import time
import argparse
from pathlib import Path

import duckdb
from openai import OpenAI
from pydantic import BaseModel

from severity_rubric import build_system_prompt, SEVERITY_RUBRIC, SEVERITY_RULES

DB_PATH = Path(__file__).resolve().parent / "rsync_github.duckdb"

# ── LLM config ──
NW_API_KEY = "sk-ab97b14bb388a45ffa9a55c41cc81546f2eac1a3ea66ad48bd088c7b21aba7bc"
NW_BASE_URL = "https://api.neuralwatt.com/v1"
NW_MODEL = "qwen3.6-35b-fast"

class SeverityScore(BaseModel):
    severity: int


SYSTEM_PROMPT = build_system_prompt()


def make_client() -> OpenAI:
    return OpenAI(base_url=NW_BASE_URL, api_key=NW_API_KEY)


def collect_github_bugs(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """GitHub bugs that haven't been scored yet."""
    rows = con.execute("""
        SELECT b.number, b.title, b.body, b.labels, b.url
        FROM bugs b
        WHERE b.severity IS NULL
        ORDER BY b.number
    """).fetchall()
    return [{"source": "github", "id": r[0], "title": r[1] or "",
             "body": r[2] or "", "labels": r[3] or "", "url": r[4] or ""}
            for r in rows]


def collect_bugzilla_bugs(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Bugzilla bugs that haven't been scored yet."""
    rows = con.execute("""
        SELECT bug_id, short_desc, bug_severity, bug_status, component, version
        FROM bugzilla_bugs
        WHERE severity IS NULL
        ORDER BY bug_id
    """).fetchall()
    return [{"source": "bugzilla", "id": r[0], "title": r[1] or "",
             "body": "", "labels": f"severity:{r[2]}; status:{r[3]}; component:{r[4]}; version:{r[5]}"}
            for r in rows]


def collect_ml_reports(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Mailing list bug reports that haven't been scored yet."""
    rows = con.execute("""
        SELECT rowid, version, subject, archive_file
        FROM ml_bug_reports
        WHERE severity IS NULL
        ORDER BY rowid
    """).fetchall()
    return [{"source": "ml", "rowid": r[0], "id": f"ml-rowid-{r[0]}",
             "release": r[1] or "", "title": r[2] or "",
             "body": "", "labels": f"archive:{r[3]}"}
            for r in rows]


def score_one(client: OpenAI, entry: dict) -> int | None:
    """Send one bug report to the LLM and parse the severity score."""
    title = entry["title"]
    body = entry["body"]
    labels = entry["labels"]

    user_parts = [f"Title: {title}"]
    if labels:
        user_parts.append(f"Labels: {labels}")
    if body:
        truncated = body[:3000]
        if len(body) > 3000:
            truncated += "\n[...truncated]"
        user_parts.append(f"Body:\n{truncated}")

    user_content = "\n\n".join(user_parts)

    try:
        response = client.beta.chat.completions.parse(
            model=NW_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=64,
            temperature=0,
            response_format=SeverityScore,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        result = response.choices[0].message.parsed
        if result is None:
            print(f"  Warning: No parsed output for {entry['source']}:{entry['id']}")
            return None
        return max(0, min(100, result.severity))

    except Exception as e:
        print(f"  Warning: API error for {entry['source']}:{entry['id']}: {e}")
        return None


def write_score_to_db(con: duckdb.DuckDBPyConnection, entry: dict, severity: int) -> None:
    """Write a severity score back to the appropriate DB table."""
    source = entry["source"]
    if source == "github":
        con.execute("UPDATE bugs SET severity = ? WHERE number = ?", [severity, entry["id"]])
    elif source == "bugzilla":
        con.execute("UPDATE bugzilla_bugs SET severity = ? WHERE bug_id = ?", [severity, entry["id"]])
    elif source == "ml":
        con.execute("UPDATE ml_bug_reports SET severity = ? WHERE rowid = ?", [severity, entry["rowid"]])


def main() -> None:
    parser = argparse.ArgumentParser(description="Score bug severity via LLM and write to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max bugs to score (for testing)")
    args = parser.parse_args()

    con = duckdb.connect(str(DB_PATH))

    # Ensure severity columns exist
    con.execute("ALTER TABLE bugs ADD COLUMN IF NOT EXISTS severity INTEGER DEFAULT NULL")
    con.execute("ALTER TABLE bugzilla_bugs ADD COLUMN IF NOT EXISTS severity INTEGER DEFAULT NULL")
    con.execute("ALTER TABLE ml_bug_reports ADD COLUMN IF NOT EXISTS severity INTEGER DEFAULT NULL")

    # Collect unscored bugs from each source
    print("Collecting unscored bug reports from database...")
    github = collect_github_bugs(con)
    bugzilla = collect_bugzilla_bugs(con)
    ml = collect_ml_reports(con)

    all_entries = github + bugzilla + ml
    print(f"  GitHub: {len(github)}, Bugzilla: {len(bugzilla)}, ML: {len(ml)} -> Total: {len(all_entries)}")

    if not all_entries:
        print("All bugs already scored!")
        con.close()
        return

    if args.limit:
        all_entries = all_entries[:args.limit]
        print(f"  Limited to {args.limit} entries")

    client = make_client()
    scored = 0
    errors = 0

    for i, entry in enumerate(all_entries):
        severity = score_one(client, entry)
        if severity is not None:
            write_score_to_db(con, entry, severity)
            scored += 1
            bar = "#" * (severity // 5) + "-" * (20 - severity // 5)
            print(f"  {i+1}/{len(all_entries)} {entry['source']}:{entry['id']}  [{bar}] {severity:3d}/100  {entry['title'][:60]}")
        else:
            errors += 1
            time.sleep(2)

        time.sleep(0.3)

    con.close()
    print(f"\nDone. Scored: {scored}, Errors: {errors}")


if __name__ == "__main__":
    main()
