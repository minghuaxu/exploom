"""Failure auto-classification engine and stale-log detection."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from exploom.config import FailureRule

# Valid status values
ATTEMPT_STATUSES = (
    "running",
    "done",
    "failed_oom",
    "failed_disk",
    "failed_quota",
    "failed_net",
    "failed_code",
    "aborted",
)

EXPERIMENT_OVERRIDE_STATUSES = ("archived", "superseded")

FAILED_STATUSES = frozenset(
    {
        "failed_oom",
        "failed_disk",
        "failed_quota",
        "failed_net",
        "failed_code",
    }
)

RESUMABLE_FAILURE_STATUSES = frozenset(
    {
        "failed_oom",
        "failed_disk",
        "failed_quota",
        "failed_net",
    }
)

FINDING_KINDS = ("analysis", "diagnosis", "decision", "artifact")

MAX_ERROR_SUMMARY_LEN = 300

# Built-in rules in priority order (DESIGN.md section 4.2)
BUILTIN_FAILURE_RULES: tuple[FailureRule, ...] = (
    FailureRule(
        status="failed_oom",
        pattern=r"CUDA out of memory|torch\.cuda\.OutOfMemoryError|OutOfMemoryError|\bOUT_OF_MEMORY\b",
        name="oom",
    ),
    FailureRule(
        status="failed_disk",
        pattern=r"No space left on device|Disk quota exceeded|\bENOSPC\b",
        name="disk",
    ),
    FailureRule(
        status="failed_quota",
        pattern=r"insufficient_quota|\b429\b|RateLimit|(?-i:\bPREEMPTED\b)|(?-i:\bTIMEOUT\b)|DUE TO TIME LIMIT",
        name="quota",
    ),
    FailureRule(
        status="failed_net",
        pattern=(
            r"NCCL.*timeout|ConnectionResetError|ReadTimeout|"
            r"BrokenPipeError|Temporary failure in name resolution"
        ),
        name="net",
    ),
    FailureRule(
        status="failed_code",
        pattern=r"Traceback \(most recent call last\)|Traceback|RuntimeError|AssertionError|Loss is NaN",
        name="code",
    ),
)


def truncate_summary(text: str, max_len: int = MAX_ERROR_SUMMARY_LEN) -> str:
    """Compact whitespace and truncate error summary to max_len characters."""
    compact = " | ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3].rstrip() + "..."


def _find_matching_line(log_tail: str, pattern: str) -> str | None:
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None
    # Search from bottom up so we capture the most recent matching error line
    lines = log_tail.splitlines()
    for line in reversed(lines):
        if regex.search(line):
            return line.strip()
    if regex.search(log_tail):
        return _tail_snippet(log_tail)
    return None


def _tail_snippet(log_tail: str, max_lines: int = 3) -> str:
    non_empty = [line.strip() for line in log_tail.splitlines() if line.strip()]
    if not non_empty:
        return ""
    return truncate_summary("\n".join(non_empty[-max_lines:]))


def classify_result(
    exit_code: int | None,
    log_tail: str = "",
    custom_rules: Iterable[FailureRule] | None = None,
) -> tuple[str, str | None]:
    """Classify an attempt outcome into (status, error_summary).

    Evaluation order (DESIGN.md section 4.2):
    1. exit_code == 0 -> done
    2. User custom rules from config.toml
    3. Built-in OOM exit codes (137, -9) and built-in regex rules
    4. Fallback -> failed_code
    """
    if exit_code == 0:
        return "done", None

    # 1. Custom rules take precedence over built-ins
    if custom_rules and log_tail:
        for rule in custom_rules:
            matched = _find_matching_line(log_tail, rule.pattern)
            if matched is not None:
                return rule.status, truncate_summary(matched)

    # 2. Exit code OOM signal (SIGKILL / 128+9)
    if exit_code in (137, -9):
        summary = _tail_snippet(log_tail) or f"Process killed with exit code {exit_code} (likely OOM)"
        return "failed_oom", truncate_summary(summary)

    # 3. Built-in regex rules
    if log_tail:
        for rule in BUILTIN_FAILURE_RULES:
            matched = _find_matching_line(log_tail, rule.pattern)
            if matched is not None:
                return rule.status, truncate_summary(matched)

    # 4. Fallback
    summary = _tail_snippet(log_tail)
    if not summary and exit_code is not None:
        summary = f"Exited with code {exit_code}"
    return "failed_code", truncate_summary(summary) if summary else None


def parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO8601 timestamp string into a timezone-aware UTC datetime."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def check_stale(
    status: str,
    log_mtime: str | None,
    threshold_minutes: int = 45,
    now: datetime | None = None,
) -> tuple[bool, int | None]:
    """Determine whether a running attempt's log has gone stale.

    Returns (is_stale, silent_minutes).
    """
    if status != "running" or not log_mtime:
        return False, None
    mtime_dt = parse_iso_timestamp(log_mtime)
    if mtime_dt is None:
        return False, None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    delta_minutes = int((current - mtime_dt).total_seconds() // 60)
    if delta_minutes < 0:
        return False, 0
    return delta_minutes > threshold_minutes, delta_minutes


def format_duration_minutes(minutes: int) -> str:
    """Format a duration in minutes into a compact human string like '45m', '2h', '1d'."""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"
