"""
Shared data models and helpers for the weekly project journal compiler.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

JOURNAL_TIMEZONE = os.environ.get("JOURNAL_TIMEZONE", "America/Los_Angeles")

SUMMARY_HEADING = "--- WEEKLY SUMMARY (AUTO-GENERATED) ---"
DETAIL_LOG_HEADING = "--- DETAILED ACTIVITY LOG ---"
LEGACY_HEADING = "--- LEGACY ENTRIES (ARCHIVED) ---"

DURATION_ROW_COUNTS = {
    "1.0": 1,
    "0.5": 2,
    "0.25": 4,
}

TASK_CATEGORY_LABELS = {
    "cad_modeling": "CAD / BIM Modeling",
    "permitting": "Permitting / Code Review",
    "engineering": "Engineering / Calcs",
}


@dataclass
class LogEntry:
    timestamp: datetime
    user: str
    hours: float
    category: str
    activity: str
    project_key: str = ""

    @property
    def timestamp_str(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %I:%M %p")

    @property
    def category_label(self) -> str:
        return TASK_CATEGORY_LABELS.get(self.category, self.category.replace("_", " ").title())

    def to_log_line(self) -> str:
        activity = self.activity.replace("\n", " ").strip()
        return (
            f"ENTRY|{self.timestamp_str}|{self.user}|{self.hours}|"
            f"{self.category}|{activity}\n"
        )

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp_str,
            "user": self.user,
            "hours": self.hours,
            "category": self.category_label,
            "category_key": self.category,
            "activity": self.activity,
            "project_key": self.project_key,
        }


def parse_log_line(line: str) -> Optional[LogEntry]:
    stripped = line.strip()
    if not stripped.startswith("ENTRY|"):
        return None

    parts = stripped.split("|", 5)
    if len(parts) < 6:
        return None

    _, timestamp_raw, user, hours_raw, category, activity = parts
    try:
        timestamp = datetime.strptime(timestamp_raw.strip(), "%Y-%m-%d %I:%M %p")
        timestamp = timestamp.replace(tzinfo=ZoneInfo(JOURNAL_TIMEZONE))
        hours = float(hours_raw.strip())
    except (ValueError, TypeError):
        return None

    return LogEntry(
        timestamp=timestamp,
        user=user.strip(),
        hours=hours,
        category=normalize_category_key(category),
        activity=activity.strip(),
    )


def normalize_category_key(category: str) -> str:
    """Map stored category keys or display labels onto a canonical key."""
    raw = (category or "").strip()
    if not raw:
        return raw
    if raw in TASK_CATEGORY_LABELS:
        return raw

    label_to_key = {label: key for key, label in TASK_CATEGORY_LABELS.items()}
    if raw in label_to_key:
        return label_to_key[raw]

    lowered = raw.lower()
    for key, label in TASK_CATEGORY_LABELS.items():
        if key.lower() == lowered or label.lower() == lowered:
            return key
    return raw


def get_current_week_range(
    reference: Optional[datetime] = None,
) -> tuple[datetime, datetime, str]:
    tz = ZoneInfo(JOURNAL_TIMEZONE)
    now = reference or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    monday = now.date() - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    week_start = datetime.combine(monday, time.min, tzinfo=tz)
    week_end = datetime.combine(sunday, time.max, tzinfo=tz)
    week_label = format_week_label(monday, sunday)
    return week_start, week_end, week_label


def format_week_label(week_start: date, week_end: date) -> str:
    if week_start.month == week_end.month:
        return f"{week_start.strftime('%B')} {week_start.day}–{week_end.day}, {week_end.year}"
    return (
        f"{week_start.strftime('%B %d')}–{week_end.strftime('%B %d, %Y')}"
    )


def filter_entries_for_week(
    entries: list[LogEntry],
    week_start: datetime,
    week_end: datetime,
) -> list[LogEntry]:
    tz = ZoneInfo(JOURNAL_TIMEZONE)
    filtered = []
    for entry in entries:
        ts = entry.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz)
        if week_start <= ts <= week_end:
            filtered.append(entry)
    return sorted(filtered, key=lambda e: e.timestamp)


def compute_hours_by_category(entries: list[LogEntry]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for entry in entries:
        label = entry.category_label
        totals[label] = round(totals.get(label, 0.0) + entry.hours, 2)
    return dict(sorted(totals.items()))


def extract_active_detail_log_text(doc_text: str) -> str:
    """
    Return only the active detailed-activity text used for weekly totals.

    Ignores archived legacy body content, while still including:
    - entries between the detail heading and the legacy heading, and
    - trailing ENTRY lines appended after the legacy block (older layout).
    """
    text = doc_text or ""
    if DETAIL_LOG_HEADING in text:
        text = text.split(DETAIL_LOG_HEADING, 1)[1]

    if LEGACY_HEADING not in text:
        return text

    before_legacy, _, after_legacy = text.partition(LEGACY_HEADING)
    trailing_entries: list[str] = []
    for line in reversed(after_legacy.splitlines()):
        stripped = line.strip()
        if stripped.startswith("ENTRY|"):
            trailing_entries.append(stripped)
        elif not stripped:
            continue
        else:
            break
    trailing_entries.reverse()

    parts = [before_legacy.strip()]
    if trailing_entries:
        parts.append("\n".join(trailing_entries))
    return "\n".join(part for part in parts if part)


def build_fallback_summary(entries: list["LogEntry"]) -> dict:
    """Python-only summary when Gemini is unavailable."""
    total_hours = round(sum(entry.hours for entry in entries), 2)
    hours_by_category = compute_hours_by_category(entries)

    lines = []
    for entry in entries:
        lines.append(
            f"- {entry.timestamp_str} ({entry.hours:g} hr, {entry.category_label}): "
            f"{entry.activity}"
        )
    narrative = (
        "Weekly activity summary compiled from logged entries:\n\n"
        + "\n".join(lines)
    )
    return {
        "total_hours": total_hours,
        "hours_by_category": hours_by_category,
        "accomplishments_narrative": narrative,
    }


def render_summary_body(
    total_hours: float,
    hours_by_category: dict[str, float],
    accomplishments_narrative: str,
) -> str:
    lines = [
        f"Total Hours: {total_hours:g}",
        "",
        "Hours by Category:",
    ]
    for category, hours in hours_by_category.items():
        padding = max(1, 28 - len(category))
        lines.append(f"  {category} {'.' * padding} {hours:g} hrs")

    lines.extend(
        [
            "",
            "Accomplishments:",
            accomplishments_narrative.strip(),
        ]
    )
    return "\n".join(lines) + "\n"
