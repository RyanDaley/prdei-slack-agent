"""
Firestore data store for Projects, Tasks, Categories, and Users.

Collections (document ID = Slack-friendly key, except users use slack_user_id):
  projects/{id}     { name, drive_folder_url }
  tasks/{id}        { name }
  categories/{id}   { name }
  users/{slack_id}  { slack_user_id, display_name, timesheet_folder_url, email }

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
class UserRecord:
    slack_user_id: str
    display_name: str
    timesheet_folder_url: str = ""
    email: str = ""


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
    return upsert_project(project_id, name, drive_folder_url)


# --- Tasks / Categories -------------------------------------------------------

def list_tasks() -> list[NamedRecord]:
    docs = get_db().collection(COL_TASKS).stream()
    items = [
        NamedRecord(id=doc.id, name=str((doc.to_dict() or {}).get("name") or doc.id))
        for doc in docs
    ]
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


def create_named(collection: str, name: str) -> NamedRecord:
    base = slugify(name)
    item_id = base
    coll = get_db().collection(collection)
    suffix = 2
    while coll.document(item_id).get().exists:
        item_id = f"{base}_{suffix}"
        suffix += 1
    return upsert_named(collection, item_id, name)


def create_task(name: str) -> NamedRecord:
    return create_named(COL_TASKS, name)


def create_category(name: str) -> NamedRecord:
    return create_named(COL_CATEGORIES, name)


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


def collection_is_empty(collection: str) -> bool:
    docs = get_db().collection(collection).limit(1).stream()
    return next(docs, None) is None


def seed_if_empty() -> dict[str, int]:
    """
    Seed default Projects / Tasks / Categories / User when collections are empty.
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

    if collection_is_empty(COL_TASKS):
        for task_id, name in [
            ("project_management", "Project Management"),
            ("schematic_design", "Schematic Design"),
            ("design_development", "Design Development"),
            ("construction_documents", "Construction Documents"),
        ]:
            upsert_named(COL_TASKS, task_id, name)
            seeded["tasks"] += 1

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

    if any(seeded.values()):
        print(f"[FIRESTORE] Seeded defaults: {seeded}", flush=True)
    else:
        print("[FIRESTORE] Collections already seeded — skip.", flush=True)
    return seeded
