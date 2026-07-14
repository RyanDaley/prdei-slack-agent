"""
Google Sheets integration for the project activity log.

The spreadsheet is the source of truth for:
  - Detailed Activity Log rows
  - Total Hours (formula)
  - Hours by Category (QUERY formula)

Per-project sheets live in the project Drive folder and are named
"Detailed Activity Log" (see project_router.ensure_project_assets).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import google.auth
from googleapiclient.discovery import build

import journal_models as jm
import project_router

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ACTIVITY_TAB = "ActivityLog"
DASHBOARD_TAB = "Dashboard"

ACTIVITY_HEADERS = [
    "Timestamp",
    "User",
    "Hours",
    "Category",
    "Activity",
    "WeekStart",
]


def get_sheets_service():
    credentials, _ = google.auth.default(scopes=SHEETS_SCOPES)
    return build("sheets", "v4", credentials=credentials)


def spreadsheet_title_for_project(_project_name: str = "") -> str:
    return project_router.DETAILED_ACTIVITY_LOG_SHEET_NAME


def _ensure_tab(sheets, spreadsheet_id: str, title: str) -> None:
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if title in existing:
        return
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()


def _write_headers_and_dashboard(sheets, spreadsheet_id: str) -> None:
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{ACTIVITY_TAB}!A1:F1",
        valueInputOption="RAW",
        body={"values": [ACTIVITY_HEADERS]},
    ).execute()

    dashboard_rows = [
        ["Week Start", ""],
        ["Week Of", ""],
        ["Total Hours", f"=IFERROR(SUMIF({ACTIVITY_TAB}!F:F,B1,{ACTIVITY_TAB}!C:C),0)"],
        [""],
        ["Hours by Category", ""],
        [
            f'=IFERROR(QUERY({ACTIVITY_TAB}!A:F,'
            f'"select D, sum(C) where F = date \'"&TEXT(B1,"yyyy-mm-dd")&"\' '
            f'group by D label D \'Category\', sum(C) \'Hours\'",1),"No entries this week")'
        ],
    ]
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{DASHBOARD_TAB}!A1:B6",
        valueInputOption="USER_ENTERED",
        body={"values": dashboard_rows},
    ).execute()


def ensure_spreadsheet(project_name: str, spreadsheet_id: str = "") -> str:
    """
    Ensure ActivityLog + Dashboard tabs/formulas exist on an existing spreadsheet.
    spreadsheet_id should come from project_router.ensure_project_assets().
    """
    sheets = get_sheets_service()
    if not spreadsheet_id:
        raise ValueError(
            f"spreadsheet_id required for project '{project_name}'. "
            "Call project_router.ensure_project_assets() first."
        )

    _ensure_tab(sheets, spreadsheet_id, ACTIVITY_TAB)
    _ensure_tab(sheets, spreadsheet_id, DASHBOARD_TAB)
    header = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A1:F1")
        .execute()
        .get("values")
        or []
    )
    if not header:
        _write_headers_and_dashboard(sheets, spreadsheet_id)
    return spreadsheet_id


def update_dashboard_week(
    spreadsheet_id: str,
    week_start: datetime,
    week_label: str,
    sheets=None,
) -> None:
    sheets = sheets or get_sheets_service()
    week_start_date = week_start.date().isoformat()
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{DASHBOARD_TAB}!B1:B2",
        valueInputOption="USER_ENTERED",
        body={"values": [[week_start_date], [week_label]]},
    ).execute()


def append_log_entries(
    spreadsheet_id: str,
    entries: list[jm.LogEntry],
    sheets=None,
) -> bool:
    if not entries:
        return True
    sheets = sheets or get_sheets_service()
    week_start, _, _ = jm.get_current_week_range()
    week_start_date = week_start.date().isoformat()

    rows = []
    for entry in entries:
        rows.append(
            [
                entry.timestamp_str,
                entry.user,
                entry.hours,
                entry.category_label,
                entry.activity,
                week_start_date,
            ]
        )

    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{ACTIVITY_TAB}!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    return True


def read_week_entries(
    spreadsheet_id: str,
    week_start: datetime | None = None,
    week_end: datetime | None = None,
    sheets=None,
) -> list[jm.LogEntry]:
    sheets = sheets or get_sheets_service()
    if week_start is None or week_end is None:
        week_start, week_end, _ = jm.get_current_week_range()

    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A2:F")
        .execute()
    )
    values = result.get("values") or []
    entries: list[jm.LogEntry] = []
    for row in values:
        padded = list(row) + [""] * (6 - len(row))
        timestamp_raw, user, hours_raw, category, activity, _week = padded[:6]
        try:
            timestamp = datetime.strptime(timestamp_raw.strip(), "%Y-%m-%d %I:%M %p")
            timestamp = timestamp.replace(tzinfo=ZoneInfo(jm.JOURNAL_TIMEZONE))
            hours = float(hours_raw)
        except Exception:
            continue
        entries.append(
            jm.LogEntry(
                timestamp=timestamp,
                user=str(user).strip(),
                hours=hours,
                category=jm.normalize_category_key(str(category)),
                activity=str(activity).strip(),
            )
        )
    return jm.filter_entries_for_week(entries, week_start, week_end)


def get_dashboard_totals(
    spreadsheet_id: str,
    sheets=None,
) -> tuple[float, dict[str, float]]:
    sheets = sheets or get_sheets_service()
    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!A1:B40",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )
    values = result.get("values") or []
    total = 0.0
    if len(values) >= 3 and len(values[2]) >= 2:
        try:
            total = round(float(values[2][1]), 2)
        except (TypeError, ValueError):
            total = 0.0

    hours_by_category: dict[str, float] = {}
    for row in values[5:]:
        if len(row) < 2:
            continue
        label = str(row[0]).strip()
        if not label or label.lower() in {
            "category",
            "hours by category",
            "no entries this week",
        }:
            continue
        try:
            hours_by_category[label] = round(float(row[1]), 2)
        except (TypeError, ValueError):
            continue
    return total, hours_by_category


def trigger_docs_refresh(
    document_id: str,
    spreadsheet_id: str,
    project_name: str,
    webapp_url: str = "",
) -> bool:
    """
    Call the deployed Apps Script web app to rebuild Doc tables from Sheets.

    Google Docs does not expose native "Update all" for linked charts/tables.
    """
    import json
    import os
    import urllib.error
    import urllib.request

    url = (webapp_url or "").strip() or os.environ.get("DOCS_REFRESH_WEBAPP_URL", "").strip()
    if not url:
        print(
            "  [SHEETS] DOCS_REFRESH_WEBAPP_URL not set — "
            "skipping Doc table refresh. Linked charts still need a manual Update, "
            "or deploy apps_script/Code.gs as a web app."
        )
        return False

    payload = json.dumps(
        {
            "documentId": document_id,
            "spreadsheetId": spreadsheet_id,
            "projectName": project_name,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
            print(f"  [SHEETS] Docs refresh webapp response: {body[:300]}")
            return True
    except urllib.error.HTTPError as exc:
        print(f"  [SHEETS ERROR] Docs refresh HTTP {exc.code}: {exc.read()[:300]}")
        return False
    except Exception as exc:
        print(f"  [SHEETS ERROR] Docs refresh failed: {exc}")
        return False
