"""
Severity scoring rubric — single source of truth for the LLM prompt,
the HTML report table, and any other consumers.

This file has ZERO external dependencies so it can be imported from anywhere.
"""

SEVERITY_RUBRIC = [
    {"range": "90–100", "label": "Data loss / corruption", "description": "Silent data corruption or data loss. The user's files or backups are wrong and they may not notice until it's too late. Security vulnerabilities that allow remote code execution or unauthorized access."},
    {"range": "70–89", "label": "Crash / hang / broken backups", "description": "rsync crashes, hangs, or fails in a way that breaks automated backups or cron jobs. Data is not corrupted but backups are missed. High CPU or memory usage that makes rsync unusable in production. Build or compilation failures — if rsync cannot be built from source, users cannot install it at all. This is a blocking problem, not a minor inconvenience. Score these at least 70. Security vulnerabilities that expose sensitive data."},
    {"range": "50–69", "label": "Feature regression", "description": "Feature regressed — something that used to work no longer works, but there's a workaround. Performance regressions large enough to disrupt production workflows. Incorrect output that is visible (errors, wrong filenames) but doesn't corrupt data."},
    {"range": "30–49", "label": "Minor regression", "description": "Minor feature regression with easy workaround. Error messages are confusing but the operation still succeeds. Intermittent test failures. Portability issues on uncommon platforms."},
    {"range": "10–29", "label": "Cosmetic / low impact", "description": "Cosmetic issues, documentation errors, minor UX annoyances. Test-only issues that don't affect users."},
    {"range": "0", "label": "Feature request", "description": "If the issue is asking for a new feature, a change in default behavior, or a packaging suggestion — no matter how reasonable — it is NOT a bug. Score it 0."},
    {"range": "0–9", "label": "Not a real bug", "description": "Spam, off-topic, duplicate. Issues that are clearly not about rsync or are empty/meaningless."},
]

SEVERITY_RULES = [
    "Score based on the WORST realistic impact, not the best case.",
    "If the issue describes a regression (something that worked before), it is inherently more severe than a longstanding limitation.",
    "Automated backup failures are severe — production systems rely on rsync.",
    "If there's not enough information to assess, score based on the title alone and lean toward the middle of the range (40–60).",
]


def build_system_prompt() -> str:
    """Generate the LLM system prompt from the rubric data."""
    lines = [
        "You are a senior reliability engineer assessing the severity of bug reports for",
        "rsync, a critical file-transfer and backup tool used on millions of production",
        "servers worldwide. rsync failures can cause silent data corruption, broken",
        "backups, and data loss.",
        "",
        "You will be given a bug report (title, and body if available). Score its",
        "severity on a 0–100 scale based on how badly it would affect real users in",
        "production, using these guidelines:",
        "",
    ]
    for entry in SEVERITY_RUBRIC:
        lines.append(f"  {entry['range']}:  {entry['description']}")
        lines.append("")
    lines.append("Important rules:")
    for rule in SEVERITY_RULES:
        lines.append(f"- {rule}")
    return "\n".join(lines)


def build_rubric_html() -> str:
    """Generate an HTML table for the report."""
    rows = []
    for entry in SEVERITY_RUBRIC:
        rows.append(
            f'<tr>'
            f'<td class="rubric-range">{entry["range"]}</td>'
            f'<td class="rubric-label">{entry["label"]}</td>'
            f'<td>{entry["description"]}</td>'
            f'</tr>'
        )
    header = '<tr><th>Score</th><th>Category</th><th>Description</th></tr>'
    return f'<table class="rubric-table">\n<thead>{header}</thead>\n<tbody>{"".join(rows)}</tbody>\n</table>'
