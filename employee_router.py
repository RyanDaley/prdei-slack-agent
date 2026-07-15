"""
Resolve per-employee Drive folders and weekly timesheet spreadsheets.

Timesheet files are Google Sheets titled like:
  Time WE MM-DD-YY <Display Name>
where the date is this week's Saturday (week ending).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

import google.auth
from googleapiclient.discovery import build

import journal_models as jm
import project_router

SHEET_MIME = "application/vnd.google-apps.spreadsheet"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


@dataclass
class EmployeeTimesheetAssets:
    folder_id: str
    spreadsheet_id: str
    spreadsheet_title: str
    week_ending: date


def get_drive_service():
    credentials, _ = google.auth.default(scopes=SCOPES)
    return build("drive", "v3", credentials=credentials)


def get_employee_folder_url(slack_user_id: str) -> str:
    """
    Resolve an employee's timesheet Drive folder.

    Lookup order:
      1. Firestore User.timesheet_folder_url
      2. EMPLOYEE_FOLDER_<SLACK_USER_ID>
      3. EMPLOYEE_TIMESHEET_FOLDER (shared single-user / default folder)
    """
    user_id = (slack_user_id or "").strip()
    if user_id:
        try:
            import firestore_store

            record = firestore_store.get_user(user_id)
            if record and record.timesheet_folder_url:
                return record.timesheet_folder_url.strip()
        except Exception as exc:
            print(f"  [TIMESHEET] Firestore user lookup failed for {user_id}: {exc}")

        env_key = f"EMPLOYEE_FOLDER_{user_id.upper()}"
        specific = os.environ.get(env_key, "").strip()
        if specific:
            return specific
    return os.environ.get("EMPLOYEE_TIMESHEET_FOLDER", "").strip()


def get_employee_display_name(slack_user_id: str) -> str:
    """Prefer Firestore User.display_name; empty string if unset."""
    user_id = (slack_user_id or "").strip()
    if not user_id:
        return ""
    try:
        import firestore_store

        record = firestore_store.get_user(user_id)
        if record and record.display_name:
            return record.display_name.strip()
    except Exception as exc:
        print(f"  [TIMESHEET] Firestore display_name lookup failed for {user_id}: {exc}")
    return ""


def timesheet_title_for_week(week_ending: date, display_name: str) -> str:
    """Weekly timesheet title includes the employee's display name."""
    clean_name = (display_name or "Employee").strip() or "Employee"
    return f"Time WE {week_ending.strftime('%m-%d-%y')} {clean_name}"


def ensure_employee_timesheet(
    slack_user_id: str,
    display_name: str,
    week_ending: date | None = None,
) -> Optional[EmployeeTimesheetAssets]:
    """
    Find or create this week's timesheet Sheet in the employee's Drive folder.
    """
    folder_url = get_employee_folder_url(slack_user_id)
    if not folder_url:
        print(
            f"  [TIMESHEET] No Drive folder for Slack user {slack_user_id}. "
            f"Set User.timesheet_folder_url in Firestore, "
            f"EMPLOYEE_FOLDER_{slack_user_id.upper()}, or EMPLOYEE_TIMESHEET_FOLDER."
        )
        return None

    folder_id = project_router.extract_id_from_url(folder_url, "folder")
    saturday = week_ending or project_router.this_saturday()
    title_name = get_employee_display_name(slack_user_id) or display_name
    title = timesheet_title_for_week(saturday, title_name)
    drive = get_drive_service()

    spreadsheet_id = project_router._find_file_in_folder(
        folder_id, title, SHEET_MIME, drive=drive
    )
    if spreadsheet_id:
        print(f"  [TIMESHEET] Reusing '{title}' ({spreadsheet_id})")
    else:
        spreadsheet_id = project_router._create_spreadsheet_in_folder(
            folder_id, title, drive=drive
        )
        print(f"  [TIMESHEET] Created '{title}' ({spreadsheet_id})")

    return EmployeeTimesheetAssets(
        folder_id=folder_id,
        spreadsheet_id=spreadsheet_id,
        spreadsheet_title=title,
        week_ending=saturday,
    )
