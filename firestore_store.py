"""
Firestore data store for Projects, Tasks, Categories, and Users.

Collections (document ID = Slack-friendly key, except users use slack_user_id):
  projects/{id}     { name, drive_folder_url }
  tasks/{id}        { name, project_id }   # id = "{project_id}__{slug}"
  categories/{id}   { name }               # global
  users/{slack_id}  { slack_user_id, display_name, timesheet_folder_url, email, last_logtime? }

Reads prefer Firestore; callers keep env.yaml / hardcoded maps as fallback.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from google.cloud import firestore

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "prdei-ai-sandbox")

COL_PROJECTS = "projects"
COL_TASKS = "tasks"
COL_CATEGORIES = "categories"
COL_USERS = "users"

_client: firestore.Client | None = None


@dataclass
class ProjectRecord:
    id: str
    name: str
    drive_folder_url: str = ""


@dataclass
class NamedRecord:
    id: str
    name: str


@dataclass
class TaskRecord:
    id: str
    name: str
    project_id: str


@dataclass
class UserRecord:
    slack_user_id: str
    display_name: str
    timesheet_folder_url: str = ""
    email: str = ""


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


# --- Projects -----------------------------------------------------------------

def list_projects() -> list[ProjectRecord]:
    docs = get_db().collection(COL_PROJECTS).stream()
    items = [
        ProjectRecord(
            id=doc.id,
            name=str((doc.to_dict() or {}).get("name") or doc.id),
            drive_folder_url=str((doc.to_dict() or {}).get("drive_folder_url") or ""),
        )
        for doc in docs
    ]
    return sorted(items, key=lambda p: p.name.lower())


def get_project(project_id: str) -> Optional[ProjectRecord]:
    snap = get_db().collection(COL_PROJECTS).document(project_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    return ProjectRecord(
        id=snap.id,
        name=str(data.get("name") or snap.id),
        drive_folder_url=str(data.get("drive_folder_url") or ""),
    )


def upsert_project(project_id: str, name: str, drive_folder_url: str = "") -> ProjectRecord:
    ref = get_db().collection(COL_PROJECTS).document(project_id)
    payload = {
        "name": name.strip(),
        "drive_folder_url": (drive_folder_url or "").strip(),
    }
    ref.set(payload, merge=True)
    return ProjectRecord(id=project_id, name=payload["name"], drive_folder_url=payload["drive_folder_url"])


def create_project(name: str, drive_folder_url: str = "") -> ProjectRecord:
    base = slugify(name)
    project_id = base
    coll = get_db().collection(COL_PROJECTS)
    suffix = 2
    while coll.document(project_id).get().exists:
        project_id = f"{base}_{suffix}"
        suffix += 1
    record = upsert_project(project_id, name, drive_folder_url)
    return record


# --- Tasks / Categories -------------------------------------------------------

def task_doc_id(project_id: str, task_slug: str) -> str:
    """Stable unique id so the same task name can exist on many projects."""
    return f"{project_id}__{task_slug}"


def _task_from_doc(doc) -> TaskRecord:
    data = doc.to_dict() or {}
    return TaskRecord(
        id=doc.id,
        name=str(data.get("name") or doc.id),
        project_id=str(data.get("project_id") or "").strip(),
    )


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


def list_categories() -> list[NamedRecord]:
    docs = get_db().collection(COL_CATEGORIES).stream()
    items = [
        NamedRecord(id=doc.id, name=str((doc.to_dict() or {}).get("name") or doc.id))
        for doc in docs
    ]
    return sorted(items, key=lambda c: c.name.lower())


def upsert_named(collection: str, item_id: str, name: str) -> NamedRecord:
    get_db().collection(collection).document(item_id).set({"name": name.strip()}, merge=True)
    return NamedRecord(id=item_id, name=name.strip())


def upsert_task(task_id: str, name: str, project_id: str) -> TaskRecord:
    payload = {
        "name": name.strip(),
        "project_id": project_id.strip(),
    }
    get_db().collection(COL_TASKS).document(task_id).set(payload, merge=True)
    return TaskRecord(id=task_id, name=payload["name"], project_id=payload["project_id"])


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
    return upsert_task(task_id, name, project_id)


def create_category(name: str) -> NamedRecord:
    return create_named(COL_CATEGORIES, name)


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
    return UserRecord(
        slack_user_id=user_id,
        display_name=str(data.get("display_name") or ""),
        timesheet_folder_url=str(data.get("timesheet_folder_url") or ""),
        email=str(data.get("email") or ""),
    )


def upsert_user(
    slack_user_id: str,
    display_name: str,
    timesheet_folder_url: str = "",
    email: str = "",
) -> UserRecord:
    user_id = slack_user_id.strip()
    payload = {
        "slack_user_id": user_id,
        "display_name": display_name.strip(),
        "timesheet_folder_url": (timesheet_folder_url or "").strip(),
        "email": (email or "").strip(),
    }
    get_db().collection(COL_USERS).document(user_id).set(payload, merge=True)
    return UserRecord(**payload)


def get_last_logtime(slack_user_id: str) -> Optional[LastLogtime]:
    """Return the user's most recent successful /logtime submission, if any."""
    user_id = (slack_user_id or "").strip()
    if not user_id:
        return None
    snap = get_db().collection(COL_USERS).document(user_id).get()
    if not snap.exists:
        return None
    data = (snap.to_dict() or {}).get("last_logtime") or {}
    raw_entries = data.get("entries") or []
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
    Persist the last /logtime form for prefill on the next open.
    Creates/merges the user document if needed (does not require prior User seed).
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
                "entries": serialized,
            },
        },
        merge=True,
    )


def collection_is_empty(collection: str) -> bool:
    docs = get_db().collection(collection).limit(1).stream()
    return next(docs, None) is None


def seed_if_empty() -> dict[str, int]:
    """
    Seed default Projects / Categories / User when collections are empty,
    then ensure every project has its own task set.
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
        for cat_id, name in [
            ("cad_modeling", "CAD / BIM Modeling"),
            ("permitting", "Permitting / Code Review"),
            ("engineering", "Engineering / Calcs"),
        ]:
            upsert_named(COL_CATEGORIES, cat_id, name)
            seeded["categories"] += 1

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
            )
            seeded["users"] += 1

    task_stats = ensure_project_tasks()
    seeded["tasks"] = int(task_stats.get("migrated") or 0)

    if any(v for k, v in seeded.items() if k != "tasks") or task_stats.get("migrated"):
        print(f"[FIRESTORE] Seeded defaults: {seeded}", flush=True)
    else:
        print("[FIRESTORE] Collections already seeded — skip.", flush=True)
    return seeded
