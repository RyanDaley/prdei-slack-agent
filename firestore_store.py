"""
Firestore data store for Projects, Tasks, Categories, Users, and time logs.

Collections (document ID = Slack-friendly key, except users use slack_user_id):
  projects/{id}     { name, drive_folder_url, start_date, estimated_end_date,
                      activity_log_spreadsheet_id?, period_document_id?,
                      period_document_title? }
  tasks/{id}        { name, project_id, completed, estimated, avg_weekly_spend }
  categories/{id}   { name, task_id, completed, estimated }
                    # id = {task_id}__{slug}; each task has its own category set
  users/{slack_id}  { slack_user_id, display_name, timesheet_folder_url, email, rate,
                      last_logtime? }   # last_logtime = Slack form PREFILL only (overwrites)
                      # last_logtime.prefill_rows = last modal rows (NOT history)
  users/{slack_id}/activities/{auto}  # PERMANENT append-only activity history for that user
  time_logs/{auto}  { project_key, task, task_id, category, category_id, hours,
                      user_id, user_email, activity, logged_at }
                      # PERMANENT firm-wide activity (same payload as user activities)

  Every Slack /logtime row appends to BOTH time_logs and users/.../activities.
  Do not confuse with last_logtime.prefill_rows — that is only form prefill.

  completed / estimated / avg_weekly_spend are integer dollar amounts.
  start_date / estimated_end_date are YYYY-MM-DD strings (blank until set).
  rate is integer dollars/hour.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from google.cloud import firestore

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "prdei-ai-sandbox")

COL_PROJECTS = "projects"
COL_TASKS = "tasks"
COL_CATEGORIES = "categories"
COL_USERS = "users"
COL_TIME_LOGS = "time_logs"
COL_USER_ACTIVITIES = "activities"  # subcollection under users/{slack_id}

# Default billable rate ($/hr) applied when a user is created or rate is missing.
DEFAULT_USER_RATE = 159

# Project schedule defaults (blank until PM sets them on the Dashboard).
DEFAULT_PROJECT_START_DATE = ""
DEFAULT_PROJECT_END_DATE = ""

# Task spend default until PM sets Avg Weekly Spend on the Dashboard.
DEFAULT_AVG_WEEKLY_SPEND = 0

_client: firestore.Client | None = None


@dataclass
class ProjectRecord:
    id: str
    name: str
    drive_folder_url: str = ""
    start_date: str = DEFAULT_PROJECT_START_DATE
    estimated_end_date: str = DEFAULT_PROJECT_END_DATE
    activity_log_spreadsheet_id: str = ""
    period_document_id: str = ""
    period_document_title: str = ""


@dataclass
class NamedRecord:
    id: str
    name: str


@dataclass
class TaskRecord:
    id: str
    name: str
    project_id: str
    completed: int = 0
    estimated: int = 0
    avg_weekly_spend: int = DEFAULT_AVG_WEEKLY_SPEND


@dataclass
class CategoryRecord:
    id: str
    name: str
    task_id: str = ""
    completed: int = 0
    estimated: int = 0


@dataclass
class UserRecord:
    slack_user_id: str
    display_name: str
    timesheet_folder_url: str = ""
    email: str = ""
    rate: int = DEFAULT_USER_RATE


@dataclass
class TimeLogRecord:
    id: str
    project_key: str
    task: str
    category: str
    hours: float
    user_id: str
    user_email: str
    logged_at: datetime
    task_id: str = ""
    category_id: str = ""
    activity: str = ""


@dataclass
class LastLogEntry:
    project_key: str
    task: str
    category: str
    activity: str
    hours: str = "1.0"


@dataclass
class LastLogtime:
    duration: str
    entries: list[LastLogEntry]


def get_db() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=PROJECT_ID)
    return _client


def slugify(name: str) -> str:
    """Build a Slack-friendly key from a display name."""
    text = (name or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "item"


def normalize_project_date(value) -> str:
    """
    Coerce a project schedule value to YYYY-MM-DD.

    Google Sheets often stores typed/ISO dates as serial day numbers
    (e.g. 46219 == 2026-07-16). Never persist those raw serials to Firestore.
    """
    from datetime import date as date_cls
    from datetime import datetime, timedelta

    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date_cls):
        return value.isoformat()

    # Numeric Sheets/Excel serial (or float like 46219.0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = int(value)
        if 20000 <= serial <= 80000:  # ~1954–2119
            return (date_cls(1899, 12, 30) + timedelta(days=serial)).isoformat()
        return ""

    text = str(value).strip()
    if not text:
        return ""

    # Already ISO
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date_cls.fromisoformat(text).isoformat()
        except ValueError:
            return ""

    # Numeric string serial from Sheet FORMATTED_VALUE when format is General
    if re.fullmatch(r"\d{5}(?:\.0+)?", text):
        try:
            serial = int(float(text))
        except ValueError:
            return ""
        if 20000 <= serial <= 80000:
            return (date_cls(1899, 12, 30) + timedelta(days=serial)).isoformat()
        return ""

    # Common locale formats from Sheets FORMATTED_VALUE
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


# --- Projects -----------------------------------------------------------------

def list_projects() -> list[ProjectRecord]:
    docs = get_db().collection(COL_PROJECTS).stream()
    items = [_project_from_doc(doc) for doc in docs]
    return sorted(items, key=lambda p: p.name.lower())


def _project_from_doc(doc) -> ProjectRecord:
    data = doc.to_dict() or {}
    return ProjectRecord(
        id=doc.id,
        name=str(data.get("name") or doc.id),
        drive_folder_url=str(data.get("drive_folder_url") or ""),
        start_date=normalize_project_date(
            data.get("start_date") or DEFAULT_PROJECT_START_DATE
        ),
        estimated_end_date=normalize_project_date(
            data.get("estimated_end_date") or DEFAULT_PROJECT_END_DATE
        ),
        activity_log_spreadsheet_id=str(data.get("activity_log_spreadsheet_id") or ""),
        period_document_id=str(data.get("period_document_id") or ""),
        period_document_title=str(data.get("period_document_title") or ""),
    )


def get_project(project_id: str) -> Optional[ProjectRecord]:
    snap = get_db().collection(COL_PROJECTS).document(project_id).get()
    if not snap.exists:
        return None
    return _project_from_doc(snap)


def upsert_project(
    project_id: str,
    name: str,
    drive_folder_url: str = "",
    *,
    start_date: str | None = None,
    estimated_end_date: str | None = None,
) -> ProjectRecord:
    ref = get_db().collection(COL_PROJECTS).document(project_id)
    payload: dict = {
        "name": name.strip(),
        "drive_folder_url": (drive_folder_url or "").strip(),
    }
    if start_date is not None:
        payload["start_date"] = normalize_project_date(start_date)
    if estimated_end_date is not None:
        payload["estimated_end_date"] = normalize_project_date(estimated_end_date)
    snap = ref.get()
    if not snap.exists:
        payload.setdefault("start_date", DEFAULT_PROJECT_START_DATE)
        payload.setdefault("estimated_end_date", DEFAULT_PROJECT_END_DATE)
    ref.set(payload, merge=True)
    return get_project(project_id) or ProjectRecord(
        id=project_id,
        name=payload["name"],
        drive_folder_url=payload["drive_folder_url"],
        start_date=normalize_project_date(payload.get("start_date") or ""),
        estimated_end_date=normalize_project_date(
            payload.get("estimated_end_date") or ""
        ),
        activity_log_spreadsheet_id=str(
            (snap.to_dict() or {}).get("activity_log_spreadsheet_id") or ""
        )
        if snap.exists
        else "",
        period_document_id=str(
            (snap.to_dict() or {}).get("period_document_id") or ""
        )
        if snap.exists
        else "",
        period_document_title=str(
            (snap.to_dict() or {}).get("period_document_title") or ""
        )
        if snap.exists
        else "",
    )


def set_project_drive_assets(
    project_id: str,
    *,
    activity_log_spreadsheet_id: str | None = None,
    period_document_id: str | None = None,
    period_document_title: str | None = None,
) -> None:
    """Persist resolved Drive file IDs so ensure_* reuses them instead of creating dupes."""
    project_id = (project_id or "").strip()
    if not project_id:
        return
    payload: dict = {}
    if activity_log_spreadsheet_id is not None:
        payload["activity_log_spreadsheet_id"] = (activity_log_spreadsheet_id or "").strip()
    if period_document_id is not None:
        payload["period_document_id"] = (period_document_id or "").strip()
    if period_document_title is not None:
        payload["period_document_title"] = (period_document_title or "").strip()
    if not payload:
        return
    get_db().collection(COL_PROJECTS).document(project_id).set(payload, merge=True)


def set_project_schedule(
    project_id: str,
    *,
    start_date: str | None = None,
    estimated_end_date: str | None = None,
) -> None:
    """Update project schedule fields (always stored as YYYY-MM-DD or blank)."""
    project_id = (project_id or "").strip()
    if not project_id:
        return
    payload: dict = {}
    if start_date is not None:
        payload["start_date"] = normalize_project_date(start_date)
    if estimated_end_date is not None:
        payload["estimated_end_date"] = normalize_project_date(estimated_end_date)
    if not payload:
        return
    get_db().collection(COL_PROJECTS).document(project_id).set(payload, merge=True)


def repair_project_schedule_dates() -> int:
    """Rewrite any Sheets-serial or non-ISO project dates to YYYY-MM-DD."""
    updated = 0
    for doc in get_db().collection(COL_PROJECTS).stream():
        data = doc.to_dict() or {}
        raw_start = data.get("start_date", "")
        raw_end = data.get("estimated_end_date", "")
        start = normalize_project_date(raw_start)
        end = normalize_project_date(raw_end)
        payload = {}
        if str(raw_start).strip() != start:
            payload["start_date"] = start
        if str(raw_end).strip() != end:
            payload["estimated_end_date"] = end
        if payload:
            doc.reference.set(payload, merge=True)
            updated += 1
            print(
                f"[FIRESTORE] Repaired project `{doc.id}` schedule "
                f"{raw_start!r}/{raw_end!r} → {start!r}/{end!r}",
                flush=True,
            )
    return updated


def ensure_project_schedule_on_first_log(
    project_id: str,
    entry_when: datetime | None = None,
) -> bool:
    """
    On the first Slack time entry for a project, if schedule fields are still
    blank, set:
      start_date = date of that entry (project timezone calendar date)
      estimated_end_date = start_date + 10 weeks

    Does not overwrite dates the PM already set. Returns True if anything changed.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    import journal_models as jm

    project_id = (project_id or "").strip()
    if not project_id:
        return False

    proj = get_project(project_id)
    if not proj:
        return False

    start_blank = not (proj.start_date or "").strip()
    end_blank = not (proj.estimated_end_date or "").strip()
    if not start_blank and not end_blank:
        return False

    if entry_when is None:
        entry_when = datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE))
    elif entry_when.tzinfo is None:
        entry_when = entry_when.replace(tzinfo=ZoneInfo(jm.JOURNAL_TIMEZONE))
    else:
        entry_when = entry_when.astimezone(ZoneInfo(jm.JOURNAL_TIMEZONE))

    start_iso = (proj.start_date or "").strip()
    if start_blank:
        start_iso = entry_when.date().isoformat()

    end_iso = (proj.estimated_end_date or "").strip()
    if end_blank:
        try:
            from datetime import date as date_cls

            start_d = date_cls.fromisoformat(start_iso)
        except ValueError:
            start_d = entry_when.date()
            start_iso = start_d.isoformat()
        end_iso = (start_d + timedelta(weeks=10)).isoformat()

    set_project_schedule(
        project_id,
        start_date=start_iso if start_blank else None,
        estimated_end_date=end_iso if end_blank else None,
    )
    print(
        f"[FIRESTORE] Project `{project_id}` schedule defaults: "
        f"start={start_iso if start_blank else proj.start_date}, "
        f"end={end_iso if end_blank else proj.estimated_end_date}",
        flush=True,
    )
    return True


def ensure_project_schedule_fields() -> int:
    """Backfill start_date / estimated_end_date on projects missing them."""
    updated = 0
    for doc in get_db().collection(COL_PROJECTS).stream():
        data = doc.to_dict() or {}
        payload: dict = {}
        if "start_date" not in data:
            payload["start_date"] = DEFAULT_PROJECT_START_DATE
        if "estimated_end_date" not in data:
            payload["estimated_end_date"] = DEFAULT_PROJECT_END_DATE
        if not payload:
            continue
        doc.reference.set(payload, merge=True)
        updated += 1
    if updated:
        print(
            f"[FIRESTORE] ensure_project_schedule_fields: initialized {updated} project(s)",
            flush=True,
        )
    return updated


def create_project(name: str, drive_folder_url: str = "") -> ProjectRecord:
    base = slugify(name)
    project_id = base
    coll = get_db().collection(COL_PROJECTS)
    suffix = 2
    while coll.document(project_id).get().exists:
        project_id = f"{base}_{suffix}"
        suffix += 1
    return upsert_project(project_id, name, drive_folder_url)


# --- Tasks / Categories -------------------------------------------------------

def task_doc_id(project_id: str, task_slug: str) -> str:
    """Stable unique id so the same task name can exist on many projects."""
    return f"{project_id}__{task_slug}"


def category_doc_id(task_id: str, category_slug: str) -> str:
    """Stable unique id so the same category name can exist on many tasks."""
    return f"{task_id}__{category_slug}"


def _task_from_doc(doc) -> TaskRecord:
    data = doc.to_dict() or {}
    return TaskRecord(
        id=doc.id,
        name=str(data.get("name") or doc.id),
        project_id=str(data.get("project_id") or "").strip(),
        completed=_as_int(data.get("completed")),
        estimated=_as_int(data.get("estimated")),
        avg_weekly_spend=_as_int(
            data.get("avg_weekly_spend"), DEFAULT_AVG_WEEKLY_SPEND
        ),
    )


def _category_from_doc(doc) -> CategoryRecord:
    data = doc.to_dict() or {}
    return CategoryRecord(
        id=doc.id,
        name=str(data.get("name") or doc.id),
        task_id=str(data.get("task_id") or "").strip(),
        completed=_as_int(data.get("completed")),
        estimated=_as_int(data.get("estimated")),
    )


def _as_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def list_tasks(project_id: str | None = None) -> list[TaskRecord]:
    """
    List tasks. When project_id is set, only that project's tasks.
    When None, return all tasks (for label maps / migration).
    """
    coll = get_db().collection(COL_TASKS)
    if project_id:
        docs = coll.where("project_id", "==", project_id).stream()
    else:
        docs = coll.stream()
    items = [_task_from_doc(doc) for doc in docs]
    if project_id:
        items = [t for t in items if t.project_id == project_id]
    return sorted(items, key=lambda t: t.name.lower())


def list_categories(task_id: str | None = None) -> list[CategoryRecord]:
    """
    List categories. When task_id is set, only that task's categories.
    When None, return all categories (for label maps / migration).
    """
    coll = get_db().collection(COL_CATEGORIES)
    if task_id:
        docs = coll.where("task_id", "==", task_id).stream()
    else:
        docs = coll.stream()
    items = [_category_from_doc(doc) for doc in docs]
    if task_id:
        items = [c for c in items if c.task_id == task_id]
    return sorted(items, key=lambda c: c.name.lower())


def list_categories_for_project(project_id: str) -> list[CategoryRecord]:
    """All categories belonging to any task on this project."""
    project_id = (project_id or "").strip()
    if not project_id:
        return []
    out: list[CategoryRecord] = []
    seen: set[str] = set()
    for task in list_tasks(project_id):
        for cat in list_categories(task.id):
            if cat.id in seen:
                continue
            seen.add(cat.id)
            out.append(cat)
    return sorted(out, key=lambda c: (c.task_id.lower(), c.name.lower()))


def get_task(task_id: str) -> Optional[TaskRecord]:
    snap = get_db().collection(COL_TASKS).document(task_id).get()
    if not snap.exists:
        return None
    return _task_from_doc(snap)


def get_category(category_id: str) -> Optional[CategoryRecord]:
    snap = get_db().collection(COL_CATEGORIES).document(category_id).get()
    if not snap.exists:
        return None
    return _category_from_doc(snap)


def upsert_named(collection: str, item_id: str, name: str) -> NamedRecord:
    get_db().collection(collection).document(item_id).set({"name": name.strip()}, merge=True)
    return NamedRecord(id=item_id, name=name.strip())


def upsert_task(
    task_id: str,
    name: str,
    project_id: str,
    *,
    completed: int | None = None,
    estimated: int | None = None,
    avg_weekly_spend: int | None = None,
) -> TaskRecord:
    payload: dict = {
        "name": name.strip(),
        "project_id": project_id.strip(),
    }
    if completed is not None:
        payload["completed"] = _as_int(completed)
    if estimated is not None:
        payload["estimated"] = _as_int(estimated)
    if avg_weekly_spend is not None:
        payload["avg_weekly_spend"] = _as_int(
            avg_weekly_spend, DEFAULT_AVG_WEEKLY_SPEND
        )
    ref = get_db().collection(COL_TASKS).document(task_id)
    snap = ref.get()
    if not snap.exists:
        payload.setdefault("completed", 0)
        payload.setdefault("estimated", 0)
        payload.setdefault("avg_weekly_spend", DEFAULT_AVG_WEEKLY_SPEND)
    ref.set(payload, merge=True)
    return get_task(task_id) or TaskRecord(
        id=task_id,
        name=payload["name"],
        project_id=payload["project_id"],
        completed=_as_int(payload.get("completed")),
        estimated=_as_int(payload.get("estimated")),
        avg_weekly_spend=_as_int(payload.get("avg_weekly_spend")),
    )


def upsert_category(
    category_id: str,
    name: str,
    task_id: str,
    *,
    completed: int | None = None,
    estimated: int | None = None,
) -> CategoryRecord:
    payload: dict = {
        "name": name.strip(),
        "task_id": (task_id or "").strip(),
    }
    if completed is not None:
        payload["completed"] = _as_int(completed)
    if estimated is not None:
        payload["estimated"] = _as_int(estimated)
    ref = get_db().collection(COL_CATEGORIES).document(category_id)
    snap = ref.get()
    if not snap.exists:
        payload.setdefault("completed", 0)
        payload.setdefault("estimated", 0)
    ref.set(payload, merge=True)
    return get_category(category_id) or CategoryRecord(
        id=category_id,
        name=payload["name"],
        task_id=payload["task_id"],
        completed=_as_int(payload.get("completed")),
        estimated=_as_int(payload.get("estimated")),
    )


def create_named(collection: str, name: str) -> NamedRecord:
    base = slugify(name)
    item_id = base
    coll = get_db().collection(collection)
    suffix = 2
    while coll.document(item_id).get().exists:
        item_id = f"{base}_{suffix}"
        suffix += 1
    return upsert_named(collection, item_id, name)


def create_task(name: str, project_id: str) -> TaskRecord:
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required to create a task")
    base = slugify(name)
    task_id = task_doc_id(project_id, base)
    coll = get_db().collection(COL_TASKS)
    suffix = 2
    while coll.document(task_id).get().exists:
        task_id = task_doc_id(project_id, f"{base}_{suffix}")
        suffix += 1
    return upsert_task(
        task_id,
        name,
        project_id,
        completed=0,
        estimated=0,
        avg_weekly_spend=DEFAULT_AVG_WEEKLY_SPEND,
    )


def create_category(name: str, task_id: str) -> CategoryRecord:
    task_id = (task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required to create a category")
    base = slugify(name)
    category_id = category_doc_id(task_id, base)
    coll = get_db().collection(COL_CATEGORIES)
    suffix = 2
    while coll.document(category_id).get().exists:
        category_id = category_doc_id(task_id, f"{base}_{suffix}")
        suffix += 1
    return upsert_category(category_id, name, task_id, completed=0, estimated=0)


def set_task_estimated(task_id: str, estimated: int) -> None:
    get_db().collection(COL_TASKS).document(task_id).set(
        {"estimated": _as_int(estimated)}, merge=True
    )


def set_task_completed(task_id: str, completed: int) -> None:
    get_db().collection(COL_TASKS).document(task_id).set(
        {"completed": _as_int(completed)}, merge=True
    )


def set_category_estimated(category_id: str, estimated: int) -> None:
    get_db().collection(COL_CATEGORIES).document(category_id).set(
        {"estimated": _as_int(estimated)}, merge=True
    )


def increment_task_completed(task_id: str, dollars: int) -> None:
    dollars = _as_int(dollars)
    if not task_id or dollars == 0:
        return
    ref = get_db().collection(COL_TASKS).document(task_id)
    ref.set({"completed": firestore.Increment(dollars)}, merge=True)


def increment_category_completed(category_id: str, dollars: int) -> None:
    dollars = _as_int(dollars)
    if not category_id or dollars == 0:
        return
    ref = get_db().collection(COL_CATEGORIES).document(category_id)
    ref.set({"completed": firestore.Increment(dollars)}, merge=True)


def set_task_avg_weekly_spend(task_id: str, avg_weekly_spend: int) -> None:
    get_db().collection(COL_TASKS).document(task_id).set(
        {"avg_weekly_spend": _as_int(avg_weekly_spend, DEFAULT_AVG_WEEKLY_SPEND)},
        merge=True,
    )


def ensure_task_money_fields() -> int:
    """
    Ensure every task document has completed, estimated, and avg_weekly_spend.
    Missing fields are initialized to defaults; existing values are left alone.
    """
    updated = 0
    for doc in get_db().collection(COL_TASKS).stream():
        data = doc.to_dict() or {}
        payload: dict = {}
        if "completed" not in data:
            payload["completed"] = 0
        if "estimated" not in data:
            payload["estimated"] = 0
        if "avg_weekly_spend" not in data:
            payload["avg_weekly_spend"] = DEFAULT_AVG_WEEKLY_SPEND
        # Drop mistaken task-level schedule fields if present from earlier draft.
        if "start_date" in data or "estimated_end_date" in data:
            # Leave values but do not require delete; schedule lives on project.
            pass
        if not payload:
            continue
        doc.reference.set(payload, merge=True)
        updated += 1
    if updated:
        print(
            f"[FIRESTORE] ensure_task_money_fields: initialized {updated} task(s)",
            flush=True,
        )
    return updated


def sync_project_task_budget(
    project_id: str,
    *,
    estimated_by_name: dict[str, int] | None = None,
    completed_by_name: dict[str, int] | None = None,
    avg_weekly_spend_by_name: dict[str, int] | None = None,
) -> int:
    """
    Push Task Budget Estimated / avg weekly values into Firestore task docs.
    Matches tasks by display name within the given project.

    Does not create tasks from the Sheet — tasks come from Slack (+) / logtime.
    Completed $ is owned by Slack → time_logs → increment_task_completed; pass
    completed_by_name only for explicit repairs, not routine ActivityLog sync.
    Returns number of fields written.
    """
    project_id = (project_id or "").strip()
    if not project_id:
        return 0
    estimated_by_name = estimated_by_name or {}
    completed_by_name = completed_by_name or {}
    avg_weekly_spend_by_name = avg_weekly_spend_by_name or {}
    if not estimated_by_name and not completed_by_name and not avg_weekly_spend_by_name:
        return 0

    tasks = list_tasks(project_id)
    by_name = {t.name: t for t in tasks}
    written = 0

    for name, est in estimated_by_name.items():
        task = by_name.get(name)
        if not task:
            continue
        if task.estimated != _as_int(est):
            set_task_estimated(task.id, est)
            written += 1
            print(
                f"[FIRESTORE] task `{task.id}` estimated → {_as_int(est)}",
                flush=True,
            )
    for name, completed in completed_by_name.items():
        task = by_name.get(name)
        if not task:
            continue
        if task.completed != _as_int(completed):
            set_task_completed(task.id, completed)
            written += 1
            print(
                f"[FIRESTORE] task `{task.id}` completed → {_as_int(completed)}",
                flush=True,
            )
    for name, spend in avg_weekly_spend_by_name.items():
        task = by_name.get(name)
        if not task:
            continue
        if task.avg_weekly_spend != _as_int(spend):
            set_task_avg_weekly_spend(task.id, spend)
            written += 1
            print(
                f"[FIRESTORE] task `{task.id}` avg_weekly_spend → {_as_int(spend)}",
                flush=True,
            )
    return written


def ensure_task_categories() -> dict[str, int]:
    """
    Idempotent migration: copy legacy global categories (no task_id) onto each
    task that has no scoped categories yet, then delete the legacy docs.

    Does not create default categories for new tasks — each task starts empty
    and gets categories via Slack (+) or create_category.
    """
    tasks = list_tasks()
    all_docs = list(get_db().collection(COL_CATEGORIES).stream())
    all_cats = [_category_from_doc(doc) for doc in all_docs]
    legacy = [c for c in all_cats if not c.task_id]
    scoped_by_task: dict[str, list[CategoryRecord]] = {}
    for cat in all_cats:
        if cat.task_id:
            scoped_by_task.setdefault(cat.task_id, []).append(cat)

    migrated = 0
    for task in tasks:
        if scoped_by_task.get(task.id):
            continue
        if not legacy:
            continue
        for lc in legacy:
            slug = slugify(lc.name) or slugify(lc.id) or "category"
            if re.fullmatch(r"[a-z0-9_]+", lc.id) and "__" not in lc.id:
                slug = lc.id
            upsert_category(
                category_doc_id(task.id, slug),
                lc.name,
                task.id,
                completed=lc.completed,
                estimated=lc.estimated,
            )
            migrated += 1

    deleted = 0
    if migrated or (legacy and not tasks):
        for lc in legacy:
            get_db().collection(COL_CATEGORIES).document(lc.id).delete()
            deleted += 1

    result = {
        "tasks": len(tasks),
        "migrated": migrated,
        "deleted_legacy": deleted,
    }
    if migrated or deleted:
        print(f"[FIRESTORE] ensure_task_categories: {result}", flush=True)
    return result


def ensure_project_tasks() -> dict[str, int]:
    """
    Idempotent migration: copy legacy global tasks (no project_id) onto each
    project that has no scoped tasks yet, then delete the legacy docs.

    Does not create any default tasks — each project starts empty and gets
    tasks only via Slack (+) or explicit create_task calls.
    """
    projects = list_projects()
    all_docs = list(get_db().collection(COL_TASKS).stream())
    all_tasks = [_task_from_doc(doc) for doc in all_docs]
    legacy = [t for t in all_tasks if not t.project_id]
    scoped_by_project: dict[str, list[TaskRecord]] = {}
    for task in all_tasks:
        if task.project_id:
            scoped_by_project.setdefault(task.project_id, []).append(task)

    migrated = 0
    for project in projects:
        if scoped_by_project.get(project.id):
            continue
        if not legacy:
            continue
        for lt in legacy:
            slug = slugify(lt.name) or slugify(lt.id) or "task"
            if re.fullmatch(r"[a-z0-9_]+", lt.id) and "__" not in lt.id:
                slug = lt.id
            upsert_task(task_doc_id(project.id, slug), lt.name, project.id)
            migrated += 1

    deleted = 0
    for lt in legacy:
        get_db().collection(COL_TASKS).document(lt.id).delete()
        deleted += 1

    result = {
        "projects": len(projects),
        "migrated": migrated,
        "deleted_legacy": deleted,
    }
    if migrated or deleted:
        print(f"[FIRESTORE] ensure_project_tasks: {result}", flush=True)
    return result


# --- Users --------------------------------------------------------------------

def get_user(slack_user_id: str) -> Optional[UserRecord]:
    user_id = (slack_user_id or "").strip()
    if not user_id:
        return None
    snap = get_db().collection(COL_USERS).document(user_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    # Auto-backfill rate when missing or still at the old unset value (0).
    if "rate" not in data or _as_int(data.get("rate")) <= 0:
        get_db().collection(COL_USERS).document(user_id).set(
            {"rate": DEFAULT_USER_RATE}, merge=True
        )
        rate = DEFAULT_USER_RATE
    else:
        rate = _as_int(data.get("rate"), DEFAULT_USER_RATE)
    return UserRecord(
        slack_user_id=user_id,
        display_name=str(data.get("display_name") or ""),
        timesheet_folder_url=str(data.get("timesheet_folder_url") or ""),
        email=str(data.get("email") or ""),
        rate=rate,
    )


def upsert_user(
    slack_user_id: str,
    display_name: str,
    timesheet_folder_url: str = "",
    email: str = "",
    rate: int | None = None,
) -> UserRecord:
    user_id = slack_user_id.strip()
    ref = get_db().collection(COL_USERS).document(user_id)
    exists = ref.get().exists
    payload: dict = {
        "slack_user_id": user_id,
        "display_name": display_name.strip(),
        "timesheet_folder_url": (timesheet_folder_url or "").strip(),
        "email": (email or "").strip(),
    }
    if rate is not None:
        payload["rate"] = _as_int(rate, DEFAULT_USER_RATE)
    elif not exists:
        payload["rate"] = DEFAULT_USER_RATE
    ref.set(payload, merge=True)
    return get_user(user_id) or UserRecord(
        slack_user_id=user_id,
        display_name=payload["display_name"],
        timesheet_folder_url=payload["timesheet_folder_url"],
        email=payload["email"],
        rate=_as_int(payload.get("rate"), DEFAULT_USER_RATE),
    )


def ensure_user_rates() -> int:
    """
    Ensure every user document has a rate field.
    Missing or zero rates are set to DEFAULT_USER_RATE (159). Positive rates are left alone.
    """
    updated = 0
    for doc in get_db().collection(COL_USERS).stream():
        data = doc.to_dict() or {}
        if "rate" in data and _as_int(data.get("rate")) > 0:
            continue
        doc.reference.set({"rate": DEFAULT_USER_RATE}, merge=True)
        updated += 1
    if updated:
        print(
            f"[FIRESTORE] ensure_user_rates: set rate={DEFAULT_USER_RATE} on {updated} user(s)",
            flush=True,
        )
    return updated


# --- Time logs ----------------------------------------------------------------

def add_time_log(
    *,
    project_key: str,
    task: str,
    category: str,
    hours: float,
    user_id: str,
    user_email: str = "",
    logged_at: datetime | None = None,
    task_id: str = "",
    category_id: str = "",
    activity: str = "",
) -> TimeLogRecord:
    """
    Append one permanent activity document.

    Writes the same payload to:
      - time_logs/{id}                    (firm-wide history / pivots)
      - users/{user_id}/activities/{id}   (per-user history in the console)

    This is append-only. It is NOT users.last_logtime (form prefill).
    """
    when = logged_at or datetime.utcnow()
    if isinstance(when, datetime) and when.tzinfo is not None:
        # Store timezone-aware timestamps as-is; Firestore accepts them.
        pass
    payload = {
        "project_key": (project_key or "").strip(),
        "task": (task or "").strip(),  # display name for pivots
        "task_id": (task_id or "").strip(),
        "category": (category or "").strip(),  # display name
        "category_id": (category_id or "").strip(),
        "hours": float(hours),
        "user_id": (user_id or "").strip(),
        "user_email": (user_email or "").strip(),
        "activity": (activity or "").strip(),
        "logged_at": when,
    }
    db = get_db()
    log_ref = db.collection(COL_TIME_LOGS).document()
    log_ref.set(payload)

    uid = payload["user_id"]
    if uid:
        # Ensure user doc exists so the activities subcollection is visible in console.
        db.collection(COL_USERS).document(uid).set(
            {"slack_user_id": uid}, merge=True
        )
        db.collection(COL_USERS).document(uid).collection(
            COL_USER_ACTIVITIES
        ).document(log_ref.id).set(payload)

    return TimeLogRecord(
        id=log_ref.id,
        project_key=payload["project_key"],
        task=payload["task"],
        category=payload["category"],
        hours=payload["hours"],
        user_id=payload["user_id"],
        user_email=payload["user_email"],
        logged_at=when,
        task_id=payload["task_id"],
        category_id=payload["category_id"],
        activity=payload["activity"],
    )


def list_time_logs(
    *,
    user_id: str | None = None,
    project_key: str | None = None,
    limit: int = 500,
) -> list[TimeLogRecord]:
    """List permanent time_logs (newest first). Optional user / project filters."""
    query = get_db().collection(COL_TIME_LOGS)
    if user_id:
        query = query.where("user_id", "==", (user_id or "").strip())
    if project_key:
        query = query.where("project_key", "==", (project_key or "").strip())
    query = query.order_by("logged_at", direction=firestore.Query.DESCENDING).limit(
        max(1, int(limit))
    )
    out: list[TimeLogRecord] = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        out.append(
            TimeLogRecord(
                id=doc.id,
                project_key=str(data.get("project_key") or ""),
                task=str(data.get("task") or ""),
                category=str(data.get("category") or ""),
                hours=float(data.get("hours") or 0),
                user_id=str(data.get("user_id") or ""),
                user_email=str(data.get("user_email") or ""),
                logged_at=data.get("logged_at") or datetime.utcnow(),
                task_id=str(data.get("task_id") or ""),
                category_id=str(data.get("category_id") or ""),
                activity=str(data.get("activity") or ""),
            )
        )
    return out


def list_user_entries(slack_user_id: str, *, limit: int = 500) -> list[TimeLogRecord]:
    """Permanent activity history under users/{id}/activities (newest first)."""
    user_id = (slack_user_id or "").strip()
    if not user_id:
        return []
    query = (
        get_db()
        .collection(COL_USERS)
        .document(user_id)
        .collection(COL_USER_ACTIVITIES)
        .order_by("logged_at", direction=firestore.Query.DESCENDING)
        .limit(max(1, int(limit)))
    )
    out: list[TimeLogRecord] = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        out.append(
            TimeLogRecord(
                id=doc.id,
                project_key=str(data.get("project_key") or ""),
                task=str(data.get("task") or ""),
                category=str(data.get("category") or ""),
                hours=float(data.get("hours") or 0),
                user_id=str(data.get("user_id") or user_id),
                user_email=str(data.get("user_email") or ""),
                logged_at=data.get("logged_at") or datetime.utcnow(),
                task_id=str(data.get("task_id") or ""),
                category_id=str(data.get("category_id") or ""),
                activity=str(data.get("activity") or ""),
            )
        )
    return out


def _resolve_log_task(entry) -> tuple[str, str]:
    """
    Return (task_id, task_display_name) from a LogEntry-like object.
    Ensures a Firestore task doc exists for the project when possible.
    """
    project_key = str(getattr(entry, "project_key", "") or "").strip()
    task_id = str(getattr(entry, "task", "") or "").strip()
    task_name = ""
    if hasattr(entry, "task_label"):
        try:
            task_name = str(entry.task_label or "").strip()
        except Exception:
            task_name = ""
    if not task_name:
        task_name = task_id.replace("_", " ").title() if task_id else ""

    # Legacy bare slugs → project-scoped ids.
    if (
        project_key
        and task_id
        and "__" not in task_id
        and re.fullmatch(r"[a-z0-9_]+", task_id)
    ):
        task_id = task_doc_id(project_key, task_id)

    if project_key and task_id:
        existing = get_task(task_id)
        if existing:
            task_name = existing.name or task_name
        else:
            # Create so every logged task is represented in Firestore.
            created = upsert_task(task_id, task_name or task_id, project_key)
            task_name = created.name
            print(
                f"[FIRESTORE] Ensured task `{task_id}` ({task_name!r}) on log",
                flush=True,
            )
    elif project_key and task_name and not task_id:
        created = create_task(task_name, project_key)
        task_id = created.id
        task_name = created.name

    return task_id, task_name


def _resolve_log_category(entry) -> tuple[str, str]:
    """Return (category_id, category_display_name). Empty when category is optional/blank."""
    category_id = str(getattr(entry, "category", "") or "").strip()
    if category_id.startswith("_"):
        # Slack placeholders: _none, _need_task, _no_categories
        return "", ""
    category_name = ""
    if hasattr(entry, "category_label"):
        try:
            category_name = str(entry.category_label or "").strip()
        except Exception:
            category_name = ""
    if not category_name and category_id:
        category_name = category_id.replace("_", " ").title()
    if category_id:
        existing = get_category(category_id)
        if existing:
            category_name = existing.name or category_name
        else:
            # Unknown / cleared — do not invent a category.
            return "", ""
    return category_id, category_name


def apply_logtime_billing(
    *,
    user_id: str,
    entries: list,
) -> list[str]:
    """
    For each Slack log entry (primary activity write path):
      1. Ensure Task exists in Firestore
      2. Append time_logs document
      3. Increment Task.completed and Category.completed by hours × rate

    Sheets/ActivityLog are updated separately as a display/copy — they are not
    the source of truth for completed $.
    """
    notes: list[str] = []
    user = get_user(user_id)
    rate = user.rate if user else DEFAULT_USER_RATE
    email = (user.email if user else "") or ""
    if rate <= 0:
        rate = DEFAULT_USER_RATE
        notes.append(f"user {user_id} had rate<=0; using default {DEFAULT_USER_RATE}")

    written = 0
    for entry in entries:
        project_key = str(getattr(entry, "project_key", "") or "").strip()
        try:
            hours = float(getattr(entry, "hours", 0) or 0)
        except (TypeError, ValueError):
            hours = 0.0
        if not project_key or hours <= 0:
            continue

        task_id, task_name = _resolve_log_task(entry)
        category_id, category_name = _resolve_log_category(entry)
        activity = str(getattr(entry, "activity", "") or "").strip()
        logged_at = getattr(entry, "timestamp", None)

        try:
            add_time_log(
                project_key=project_key,
                task=task_name or task_id,
                task_id=task_id,
                category=category_name or category_id,
                category_id=category_id,
                hours=hours,
                user_id=user_id,
                user_email=email,
                activity=activity,
                logged_at=logged_at if isinstance(logged_at, datetime) else None,
            )
            written += 1
        except Exception as exc:
            notes.append(f"time_log failed ({task_name or task_id}): {exc}")
            continue

        dollars = int(round(hours * rate))
        if dollars <= 0:
            continue
        if task_id:
            try:
                increment_task_completed(task_id, dollars)
            except Exception as exc:
                notes.append(f"task completed increment failed ({task_id}): {exc}")
        if category_id:
            try:
                increment_category_completed(category_id, dollars)
            except Exception as exc:
                notes.append(
                    f"category completed increment failed ({category_id}): {exc}"
                )

    notes.append(f"wrote {written}/{len(entries)} time_log(s) for user {user_id}")
    return notes


def get_last_logtime(slack_user_id: str) -> Optional[LastLogtime]:
    """Return the user's last /logtime form snapshot for Slack prefill, if any."""
    user_id = (slack_user_id or "").strip()
    if not user_id:
        return None
    snap = get_db().collection(COL_USERS).document(user_id).get()
    if not snap.exists:
        return None
    data = (snap.to_dict() or {}).get("last_logtime") or {}
    # New key: prefill_rows. Legacy key: entries (nested under last_logtime only).
    raw_entries = data.get("prefill_rows") or data.get("entries") or []
    if not isinstance(raw_entries, list) or not raw_entries:
        return None
    entries: list[LastLogEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        project_key = str(item.get("project_key") or "").strip()
        if not project_key:
            continue
        entries.append(
            LastLogEntry(
                project_key=project_key,
                task=str(item.get("task") or "").strip(),
                category=str(item.get("category") or "").strip(),
                activity=str(item.get("activity") or "").strip(),
                hours=str(item.get("hours") or data.get("duration") or "1.0").strip()
                or "1.0",
            )
        )
    if not entries:
        return None
    duration = str(data.get("duration") or entries[0].hours or "1.0").strip() or "1.0"
    return LastLogtime(duration=duration, entries=entries)


def set_last_logtime(
    slack_user_id: str,
    duration: str,
    entries: list[LastLogEntry] | list[dict],
) -> None:
    """
    Save the last /logtime FORM for Slack prefill only.

    Stored as users/{id}.last_logtime.prefill_rows (overwritten each submit).
    Permanent activity history is a separate subcollection:
      users/{id}/activities/{auto}
    plus the firm-wide collection:
      time_logs/{auto}
    """
    user_id = (slack_user_id or "").strip()
    if not user_id or not entries:
        return
    serialized = []
    for item in entries:
        if isinstance(item, LastLogEntry):
            serialized.append(
                {
                    "project_key": item.project_key,
                    "task": item.task,
                    "category": item.category,
                    "activity": item.activity,
                    "hours": (item.hours or "1.0").strip() or "1.0",
                }
            )
        elif isinstance(item, dict):
            project_key = str(item.get("project_key") or "").strip()
            if not project_key:
                continue
            serialized.append(
                {
                    "project_key": project_key,
                    "task": str(item.get("task") or "").strip(),
                    "category": str(item.get("category") or "").strip(),
                    "activity": str(item.get("activity") or "").strip(),
                    "hours": str(item.get("hours") or "1.0").strip() or "1.0",
                }
            )
    if not serialized:
        return
    get_db().collection(COL_USERS).document(user_id).set(
        {
            "slack_user_id": user_id,
            "last_logtime": {
                "duration": (duration or "1.0").strip() or "1.0",
                "prefill_rows": serialized,
                # Drop the old nested "entries" key so it is not mistaken for history.
                "entries": firestore.DELETE_FIELD,
            },
        },
        merge=True,
    )


def collection_is_empty(collection: str) -> bool:
    docs = get_db().collection(collection).limit(1).stream()
    return next(docs, None) is None


def seed_if_empty() -> dict[str, int]:
    """
    Seed default Projects / User when collections are empty,
    migrate legacy global tasks/categories onto projects/tasks,
    then ensure money/schedule fields exist.
    Safe to call on every startup.
    """
    import project_router

    seeded = {"projects": 0, "tasks": 0, "categories": 0, "users": 0}

    if collection_is_empty(COL_PROJECTS):
        defaults = [
            ("tahoe_backyard", "Tahoe Backyard"),
            ("wood_energy_facility", "Wood Energy Facility"),
            ("8494_speckled", "8494 Speckled Ave"),
        ]
        for project_id, name in defaults:
            env_key = f"PROJECT_FOLDER_{project_id.upper()}"
            folder = (
                os.environ.get(env_key, "").strip()
                or project_router.PROJECT_DOC_MAP.get(project_id, "")
            )
            upsert_project(project_id, name, folder)
            seeded["projects"] += 1

    if collection_is_empty(COL_CATEGORIES):
        # No global category seed — categories are created per task via Slack (+).
        pass

    if collection_is_empty(COL_USERS):
        slack_id = os.environ.get("REMINDER_USER_ID", "").strip()
        email = os.environ.get("REMINDER_USER_EMAIL", "").strip()
        folder = (
            os.environ.get(f"EMPLOYEE_FOLDER_{slack_id.upper()}", "").strip()
            if slack_id
            else ""
        ) or os.environ.get("EMPLOYEE_TIMESHEET_FOLDER", "").strip()
        if slack_id:
            upsert_user(
                slack_user_id=slack_id,
                display_name="Ryan Daley",
                timesheet_folder_url=folder,
                email=email,
                rate=DEFAULT_USER_RATE,
            )
            seeded["users"] += 1

    task_stats = ensure_project_tasks()
    seeded["tasks"] = int(task_stats.get("migrated") or 0)
    cat_stats = ensure_task_categories()
    seeded["categories"] = int(cat_stats.get("migrated") or 0)
    ensure_user_rates()
    ensure_task_money_fields()
    ensure_project_schedule_fields()
    repaired = repair_project_schedule_dates()
    if repaired:
        print(
            f"[FIRESTORE] Repaired {repaired} project schedule date(s) "
            "(Sheets serial → YYYY-MM-DD)",
            flush=True,
        )

    if any(v for k, v in seeded.items() if k not in {"tasks", "categories"}) or task_stats.get("migrated") or cat_stats.get("migrated"):
        print(f"[FIRESTORE] Seeded defaults: {seeded}", flush=True)
    else:
        print("[FIRESTORE] Collections already seeded — skip.", flush=True)
    return seeded
