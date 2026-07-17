"""
AEC Project Router Utility.

Each project points at a Google Drive folder. Inside that folder the agent
ensures:
  - a Google Sheet named "Detailed Activity Log" (reused; never intentionally
    duplicated — file ID is also stored on the Firestore project)
  - a Google Doc for the current 2-week period, titled with this Saturday's
    date (YYYY-MM-DD). Existing docs titled with this Saturday OR next
    Saturday are reused. A new Doc is only created when the biweekly period
    rolls to a new Saturday title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import google.auth
from googleapiclient.discovery import build

import journal_models as jm

# Google Drive folder URL or bare folder ID for each project.
# Example: "https://drive.google.com/drive/folders/FOLDER_ID"
PROJECT_DOC_MAP = {
    "tahoe_backyard": "https://drive.google.com/drive/folders/1qkX-21aRpV_reWk6dFAJj0BZNQL1Y6QO?usp=sharing",
    "wood_energy_facility": "https://drive.google.com/drive/folders/1jheqWCs3igN9oIcHE91J1WPStvavP5ZR?usp=sharing",
    "8494_speckled": "https://drive.google.com/drive/folders/1rZ-2m8x34WLNO9xHI34KQVvIikTWoYWF?usp=sharing",
}

PROJECT_DISPLAY_NAMES = {
    "tahoe_backyard": "Tahoe Backyard",
    "wood_energy_facility": "Wood Energy Facility",
    "8494_speckled": "8494 Speckled Ave",
}

# Backward-compatible alias.
PROJECT_JOURNAL_MAP = PROJECT_DOC_MAP

DETAILED_ACTIVITY_LOG_SHEET_NAME = "Detailed Activity Log"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Service accounts that must be able to read/write project journal folders.
DEFAULT_PROJECT_FOLDER_SHARE_EMAILS = (
    "312720759301-compute@developer.gserviceaccount.com",
    "service-312720759301@gcp-sa-vertex-rag.iam.gserviceaccount.com",
)


@dataclass
class ProjectAssets:
    project_key: str
    folder_id: str
    document_id: str
    spreadsheet_id: str
    document_title: str


def get_drive_service():
    credentials, _ = google.auth.default(scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=credentials)


def _drive_service_from_user_token(access_token: str):
    from google.oauth2.credentials import Credentials

    credentials = Credentials(token=access_token)
    return build("drive", "v3", credentials=credentials)


def project_folder_share_emails() -> list[str]:
    """
    Emails granted writer access on newly linked project folders.
    Override with comma-separated PROJECT_FOLDER_SHARE_EMAILS if needed.
    """
    import os

    raw = os.environ.get("PROJECT_FOLDER_SHARE_EMAILS", "").strip()
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return list(DEFAULT_PROJECT_FOLDER_SHARE_EMAILS)


def share_folder_with_service_accounts(
    folder_id: str,
    *,
    access_token: str | None = None,
    emails: list[str] | None = None,
    role: str = "writer",
) -> tuple[bool, list[str]]:
    """
    Grant the agent service accounts access to a project folder.

    Requires a user OAuth access token. The Cloud Run service account typically
    cannot see a brand-new folder until after it is shared, so ADC fallback is
    intentionally avoided (it surfaces as Drive 404 File not found).

    Returns (all_ok, notes).
    """
    targets = emails or project_folder_share_emails()
    if not folder_id or not targets:
        return True, []

    if not (access_token or "").strip():
        return False, [
            "Google sign-in is required to share the folder with the agent service accounts."
        ]

    notes: list[str] = []
    try:
        drive = _drive_service_from_user_token(access_token.strip())
    except Exception as exc:
        return False, [f"could not build Drive client from user token: {exc}"]

    # Confirm the user token can see the folder (Shared Drives need supportsAllDrives).
    try:
        drive.files().get(
            fileId=folder_id,
            fields="id,name,driveId",
            supportsAllDrives=True,
        ).execute()
    except Exception as exc:
        return False, [
            f"signed-in Google account cannot access that folder ({exc}). "
            "Open the folder in Drive with this account, or paste a folder you can share."
        ]

    all_ok = True
    for email in targets:
        try:
            drive.permissions().create(
                fileId=folder_id,
                body={
                    "type": "user",
                    "role": role,
                    "emailAddress": email,
                },
                sendNotificationEmail=False,
                supportsAllDrives=True,
                fields="id",
            ).execute()
            print(
                f"[ROUTER] Shared folder {folder_id} with {email} "
                f"as {role} via user OAuth",
                flush=True,
            )
            notes.append(f"shared with {email}")
        except Exception as exc:
            message = str(exc)
            if "alreadyExists" in message or "duplicate" in message.lower():
                print(
                    f"[ROUTER] Folder {folder_id} already shared with {email}",
                    flush=True,
                )
                notes.append(f"{email} already had access")
                continue
            all_ok = False
            notes.append(f"{email}: {exc}")
            print(
                f"[ROUTER WARNING] Could not share folder {folder_id} "
                f"with {email}: {exc}",
                flush=True,
            )
    return all_ok, notes


def extract_id_from_url(input_string: str, kind: str = "folder") -> str:
    """
    Pull a Drive folder / Doc / Sheet ID out of a URL, or return a bare ID.
    kind: "folder" | "document" | "spreadsheets"
    """
    if not input_string:
        return ""

    patterns = {
        "folder": r"/folders/([a-zA-Z0-9-_]+)",
        "document": r"/document/d/([a-zA-Z0-9-_]+)",
        "spreadsheets": r"/spreadsheets/d/([a-zA-Z0-9-_]+)",
    }
    match = re.search(patterns.get(kind, patterns["folder"]), input_string)
    if match:
        return match.group(1)

    # Bare ID (no URL path). Reject full URLs that didn't match the expected kind.
    stripped = input_string.strip()
    if "://" in stripped or "/" in stripped:
        return ""
    return stripped


def get_folder_id_for_project(project_value: str) -> Optional[str]:
    import os

    raw = ""
    # 1) Firestore project record
    try:
        import firestore_store

        record = firestore_store.get_project(project_value)
        if record and record.drive_folder_url:
            raw = record.drive_folder_url.strip()
    except Exception as exc:
        print(f"[ROUTER] Firestore project lookup failed for '{project_value}': {exc}", flush=True)

    # 2) Cloud Run env (legacy fallback until full cutover)
    env_key = f"PROJECT_FOLDER_{project_value.upper()}"
    if not raw:
        raw = os.environ.get(env_key, "").strip() or PROJECT_DOC_MAP.get(project_value, "")

    if not raw:
        print(
            f"[ROUTER ERROR] No Drive folder mapped for key '{project_value}'. "
            f"Set drive_folder_url in Firestore, {env_key} in env.yaml, "
            "or PROJECT_DOC_MAP in project_router.py.",
            flush=True,
        )
        return None
    folder_id = extract_id_from_url(raw, kind="folder")
    if not folder_id:
        print(
            f"[ROUTER ERROR] Could not parse folder ID for '{project_value}' from: {raw}",
            flush=True,
        )
        return None
    print(f"[ROUTER] Resolved '{project_value}' -> folder {folder_id}", flush=True)
    return folder_id


def get_project_display_name(project_value: str) -> str:
    try:
        import firestore_store

        record = firestore_store.get_project(project_value)
        if record and record.name:
            return record.name
    except Exception as exc:
        print(f"[ROUTER] Firestore display name lookup failed for '{project_value}': {exc}", flush=True)

    return PROJECT_DISPLAY_NAMES.get(
        project_value,
        project_value.replace("_", " ").title(),
    )


def this_saturday(reference: Optional[date] = None) -> date:
    """Upcoming Saturday (today, if today is Saturday)."""
    today = reference or datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE)).date()
    days_ahead = (5 - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


def next_saturday(reference: Optional[date] = None) -> date:
    return this_saturday(reference) + timedelta(days=7)


def period_doc_titles(reference: Optional[date] = None) -> tuple[str, str]:
    """
    Candidate titles for the current 2-week journal Doc:
    this Saturday and next Saturday as YYYY-MM-DD.
    """
    current = this_saturday(reference)
    following = next_saturday(reference)
    return current.isoformat(), following.isoformat()


def _escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _list_files_in_folder(
    folder_id: str,
    name: str,
    mime_type: str,
    drive=None,
) -> list[dict]:
    """Return all non-trashed matches (oldest first)."""
    drive = drive or get_drive_service()
    query = (
        f"'{folder_id}' in parents "
        f"and name = '{_escape_drive_query(name)}' "
        f"and mimeType = '{mime_type}' "
        "and trashed = false"
    )
    response = (
        drive.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id, name, createdTime)",
            pageSize=25,
            orderBy="createdTime",
            corpora="allDrives",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return list(response.get("files") or [])


def _find_file_in_folder(
    folder_id: str,
    name: str,
    mime_type: str,
    drive=None,
) -> Optional[str]:
    """
    Return the file ID for name+mime in folder.
    If duplicates exist, reuse the oldest and log a warning (do not create another).
    """
    files = _list_files_in_folder(folder_id, name, mime_type, drive=drive)
    if not files:
        return None
    if len(files) > 1:
        ids = ", ".join(f"{f['id']}" for f in files)
        print(
            f"  [ROUTER WARNING] Found {len(files)} files named '{name}' in folder "
            f"{folder_id}. Reusing oldest ({files[0]['id']}); delete extras: {ids}",
            flush=True,
        )
    return files[0]["id"]


def _file_still_usable(file_id: str, drive=None) -> bool:
    """True if file_id exists and is not trashed."""
    file_id = (file_id or "").strip()
    if not file_id:
        return False
    drive = drive or get_drive_service()
    try:
        meta = (
            drive.files()
            .get(
                fileId=file_id,
                fields="id, trashed",
                supportsAllDrives=True,
            )
            .execute()
        )
        return bool(meta.get("id")) and not bool(meta.get("trashed"))
    except Exception as exc:
        print(f"  [ROUTER] Stored file {file_id} not usable: {exc}", flush=True)
        return False


def _find_period_document(folder_id: str, drive=None) -> tuple[Optional[str], str]:
    """
    Look for a Doc titled this Saturday or next Saturday (YYYY-MM-DD).
    Returns (document_id_or_none, preferred_create_title).
    """
    drive = drive or get_drive_service()
    this_title, next_title = period_doc_titles()
    for title in (this_title, next_title):
        found = _find_file_in_folder(folder_id, title, DOC_MIME, drive=drive)
        if found:
            print(f"  [ROUTER] Reusing period Doc '{title}' ({found})")
            return found, title
    return None, this_title


def _create_document_in_folder(folder_id: str, title: str, drive=None) -> str:
    drive = drive or get_drive_service()
    # Re-check immediately before create to reduce duplicate races.
    existing = _find_file_in_folder(folder_id, title, DOC_MIME, drive=drive)
    if existing:
        print(f"  [ROUTER] Reusing period Doc '{title}' ({existing}) before create")
        return existing
    created = (
        drive.files()
        .create(
            body={
                "name": title,
                "mimeType": DOC_MIME,
                "parents": [folder_id],
            },
            fields="id, name",
            supportsAllDrives=True,
        )
        .execute()
    )
    # If a race created another copy, always prefer the oldest.
    oldest = _find_file_in_folder(folder_id, title, DOC_MIME, drive=drive)
    if oldest and oldest != created["id"]:
        print(
            f"  [ROUTER WARNING] Duplicate Doc '{title}' created during race; "
            f"using oldest {oldest} (new {created['id']})",
            flush=True,
        )
        return oldest
    print(f"  [ROUTER] Created period Doc '{title}' ({created['id']}) in folder {folder_id}")
    return created["id"]


def _create_spreadsheet_in_folder(folder_id: str, title: str, drive=None) -> str:
    drive = drive or get_drive_service()
    existing = _find_file_in_folder(folder_id, title, SHEET_MIME, drive=drive)
    if existing:
        print(f"  [ROUTER] Reusing spreadsheet '{title}' ({existing}) before create")
        return existing
    created = (
        drive.files()
        .create(
            body={
                "name": title,
                "mimeType": SHEET_MIME,
                "parents": [folder_id],
            },
            fields="id, name",
            supportsAllDrives=True,
        )
        .execute()
    )
    oldest = _find_file_in_folder(folder_id, title, SHEET_MIME, drive=drive)
    if oldest and oldest != created["id"]:
        print(
            f"  [ROUTER WARNING] Duplicate Sheet '{title}' created during race; "
            f"using oldest {oldest} (new {created['id']})",
            flush=True,
        )
        return oldest
    print(
        f"  [ROUTER] Created spreadsheet '{title}' ({created['id']}) in folder {folder_id}"
    )
    return created["id"]


def ensure_detailed_activity_log_sheet(
    folder_id: str,
    drive=None,
    *,
    preferred_id: str = "",
) -> str:
    drive = drive or get_drive_service()
    if preferred_id and _file_still_usable(preferred_id, drive=drive):
        print(
            f"  [ROUTER] Reusing stored Detailed Activity Log ({preferred_id})",
            flush=True,
        )
        return preferred_id
    existing = _find_file_in_folder(
        folder_id, DETAILED_ACTIVITY_LOG_SHEET_NAME, SHEET_MIME, drive=drive
    )
    if existing:
        return existing
    return _create_spreadsheet_in_folder(
        folder_id, DETAILED_ACTIVITY_LOG_SHEET_NAME, drive=drive
    )


def ensure_period_document(
    folder_id: str,
    drive=None,
    *,
    preferred_id: str = "",
    preferred_title: str = "",
) -> tuple[str, str]:
    drive = drive or get_drive_service()
    this_title, next_title = period_doc_titles()
    valid_titles = {this_title, next_title}

    # Reuse Firestore-tracked period Doc when it still matches this biweekly window.
    if (
        preferred_id
        and preferred_title in valid_titles
        and _file_still_usable(preferred_id, drive=drive)
    ):
        print(
            f"  [ROUTER] Reusing stored period Doc '{preferred_title}' ({preferred_id})",
            flush=True,
        )
        return preferred_id, preferred_title

    existing_id, create_title = _find_period_document(folder_id, drive=drive)
    if existing_id:
        return existing_id, create_title
    return _create_document_in_folder(folder_id, create_title, drive=drive), create_title


def ensure_project_assets(project_value: str) -> Optional[ProjectAssets]:
    """
    Resolve folder + ensure Sheet/Doc exist for this project and biweekly period.
    Reuses Firestore-stored file IDs and oldest same-name Drive matches so we
    do not keep creating duplicate Detailed Activity Log / period Docs.
    """
    folder_id = get_folder_id_for_project(project_value)
    if not folder_id:
        return None

    preferred_sheet = ""
    preferred_doc = ""
    preferred_doc_title = ""
    try:
        import firestore_store

        record = firestore_store.get_project(project_value)
        if record:
            preferred_sheet = record.activity_log_spreadsheet_id or ""
            preferred_doc = record.period_document_id or ""
            preferred_doc_title = record.period_document_title or ""
    except Exception as exc:
        print(f"[ROUTER] Could not load stored Drive asset IDs: {exc}", flush=True)

    try:
        drive = get_drive_service()
        spreadsheet_id = ensure_detailed_activity_log_sheet(
            folder_id, drive=drive, preferred_id=preferred_sheet
        )
        document_id, document_title = ensure_period_document(
            folder_id,
            drive=drive,
            preferred_id=preferred_doc,
            preferred_title=preferred_doc_title,
        )
    except Exception as exc:
        print(
            f"[ROUTER ERROR] Failed ensuring Drive assets for '{project_value}' "
            f"in folder {folder_id}: {exc}",
            flush=True,
        )
        raise

    try:
        import firestore_store

        if (
            spreadsheet_id != preferred_sheet
            or document_id != preferred_doc
            or document_title != preferred_doc_title
        ):
            firestore_store.set_project_drive_assets(
                project_value,
                activity_log_spreadsheet_id=spreadsheet_id,
                period_document_id=document_id,
                period_document_title=document_title,
            )
    except Exception as exc:
        print(f"[ROUTER WARNING] Could not persist Drive asset IDs: {exc}", flush=True)

    return ProjectAssets(
        project_key=project_value,
        folder_id=folder_id,
        document_id=document_id,
        spreadsheet_id=spreadsheet_id,
        document_title=document_title,
    )


def get_journal_id_for_project(project_value: str) -> Optional[str]:
    """
    Ensure the biweekly period Doc exists and return its ID.
    Prefer ensure_project_assets() when both Sheet and Doc are needed.
    """
    assets = ensure_project_assets(project_value)
    return assets.document_id if assets else None


def get_sheet_id_for_project(project_value: str) -> str:
    """Ensure Detailed Activity Log sheet exists and return its ID."""
    assets = ensure_project_assets(project_value)
    return assets.spreadsheet_id if assets else ""


if __name__ == "__main__":
    print("Project folder map / period titles:")
    print(f"  this Saturday: {this_saturday().isoformat()}")
    print(f"  next Saturday: {next_saturday().isoformat()}")
    for project in PROJECT_DOC_MAP:
        folder = PROJECT_DOC_MAP[project] or "(set folder URL)"
        print(f"  ↳ {project}: folder={folder}")
