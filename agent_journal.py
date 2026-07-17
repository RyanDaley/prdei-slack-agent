"""
AEC Agent Journal Integration.

Architecture:
  - Google Sheets = source of truth for Detailed Activity Log, Total Hours,
    Hours by Task / Task Budget (Completed vs Estimated $), and the task
    progress bar chart.
  - Google Docs = client narrative (Gemini) plus Hours/Activity text synced
    from Sheets, with the task budget chart re-embedded as an image on each
    Activity Log update (Docs API cannot refresh native linked charts).
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import google.auth
from google import genai
from google.genai import types as genai_types
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

import agent_sheets
import journal_models as jm
import project_router

PROJECT_ID = "prdei-ai-sandbox"
LOCATION = "us-west1"
GEMINI_MODEL = "gemini-2.5-flash"
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HOURS_START = "Hours Summary"
CHART_START = "Category Chart"
ACTIVITY_START = "Detailed Activity Log"

# Document Heading 1: 14 pt, bold, PRDEI blue
HEADING1_RGB = (69, 176, 225)  # #45B0E1
HEADING1_FONT_PT = 14
HEADING1_TITLES = (
    "Weekly Summary",
    "Hours Summary",
    "Hours by Task:",
    "Hours by Category:",  # legacy docs
    "Category Chart",
    "Detailed Activity Log",
)

# Normal text + bold (not a separate named style)
BOLD_NORMAL_EXACT_TITLES = (
    "Accomplishments",
)


@dataclass
class JournalUpdateResult:
    success: bool
    log_appended: bool = False
    summary_updated: bool = False
    docs_refreshed: bool = False
    hours_logged: float = 0.0
    spreadsheet_id: str = ""
    error_message: str = ""


def get_docs_service():
    try:
        credentials, _ = google.auth.default(scopes=SCOPES)
        return build("docs", "v1", credentials=credentials)
    except Exception as exc:
        print(f"[AUTH ERROR] Failed to obtain Application Default Credentials: {exc}")
        print(" -> To run locally, execute: gcloud auth application-default login")
        raise exc


def _heading1_text_style() -> dict:
    r, g, b = HEADING1_RGB
    return {
        "bold": True,
        "fontSize": {"magnitude": float(HEADING1_FONT_PT), "unit": "PT"},
        "foregroundColor": {
            "color": {
                "rgbColor": {
                    "red": r / 255.0,
                    "green": g / 255.0,
                    "blue": b / 255.0,
                }
            }
        },
    }


def _style_paragraph_requests(
    start: int,
    end: int,
    named_style: str,
    text_style: dict,
    text_style_fields: str = "bold,fontSize,foregroundColor",
) -> list[dict]:
    """Build paragraph + text style update requests for one title range."""
    requests = [
        {
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": named_style},
                "fields": "namedStyleType",
            }
        }
    ]
    text_end = end - 1 if end > start else end
    if text_end > start:
        requests.append(
            {
                "updateTextStyle": {
                    "range": {"startIndex": start, "endIndex": text_end},
                    "textStyle": text_style,
                    "fields": text_style_fields,
                }
            }
        )
    return requests


def ensure_document_heading_styles(document_id: str, service=None) -> bool:
    """Define the Heading 1 named style for the document."""
    service = service or get_docs_service()
    try:
        service.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "updateNamedStyle": {
                            "namedStyle": {
                                "namedStyleType": "HEADING_1",
                                "textStyle": _heading1_text_style(),
                            },
                            "fields": (
                                "namedStyleType,"
                                "textStyle.bold,"
                                "textStyle.fontSize,"
                                "textStyle.foregroundColor"
                            ),
                        }
                    }
                ]
            },
        ).execute()
        print(
            f"  [JOURNAL] Heading 1 style set to {HEADING1_FONT_PT}pt "
            f"bold RGB{HEADING1_RGB}"
        )
        return True
    except Exception as exc:
        print(f"  [JOURNAL WARNING] Could not update heading named styles: {exc}")
        return False


def _is_bold_normal_paragraph(text: str, name_labels: set[str]) -> bool:
    stripped = text.strip()
    if stripped in BOLD_NORMAL_EXACT_TITLES:
        return True
    if stripped.startswith("Total Hours:"):
        return True
    if stripped in name_labels:
        return True
    return False


def apply_document_heading_styles(document_id: str, service=None) -> tuple[int, int]:
    """
    Apply Heading 1 to section titles, and Normal text + bold to
    Accomplishments / Total Hours / task names.
    Returns (h1_count, bold_normal_count).
    """
    service = service or get_docs_service()
    document = get_document(service, document_id)
    body_content = _collect_body_elements(document)
    h1_titles = {t.strip() for t in HEADING1_TITLES}
    name_labels = set(jm.task_labels_map().values()) | set(
        jm.category_labels_map().values()
    )
    requests: list[dict] = []
    h1_count = 0
    bold_count = 0

    for element in body_content:
        if "paragraph" not in element:
            continue
        text = _paragraph_text(element).strip()
        start = element.get("startIndex")
        end = element.get("endIndex")
        if start is None or end is None or start >= end:
            continue

        if text in h1_titles:
            requests.extend(
                _style_paragraph_requests(
                    start, end, "HEADING_1", _heading1_text_style()
                )
            )
            h1_count += 1
        elif _is_bold_normal_paragraph(text, name_labels):
            requests.extend(
                _style_paragraph_requests(
                    start,
                    end,
                    "NORMAL_TEXT",
                    {"bold": True},
                    text_style_fields="bold",
                )
            )
            bold_count += 1

    if requests:
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()
    return h1_count, bold_count


def get_document(service, document_id: str) -> dict:
    return (
        service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )


def _paragraph_text(paragraph_element: dict) -> str:
    parts = []
    for elem in paragraph_element.get("paragraph", {}).get("elements", []):
        text_run = elem.get("textRun")
        if text_run:
            parts.append(text_run.get("content", ""))
    return "".join(parts)


def _collect_body_elements(document: dict) -> list[dict]:
    tabs = document.get("tabs") or []
    if tabs:
        body = tabs[0].get("documentTab", {}).get("body", {})
    else:
        body = document.get("body", {})
    return body.get("content", [])


def read_document_text(document_id: str, service=None) -> str:
    service = service or get_docs_service()
    document = get_document(service, document_id)
    body_content = _collect_body_elements(document)

    lines = []
    for element in body_content:
        if "paragraph" in element:
            lines.append(_paragraph_text(element))
    return "".join(lines)


def _find_marker_indices(body_content: list[dict], marker: str) -> Optional[tuple[int, int]]:
    for element in body_content:
        if "paragraph" not in element:
            continue
        text = _paragraph_text(element)
        if marker in text:
            return element.get("startIndex"), element.get("endIndex")
    return None


def _build_doc_header(project_name: str, week_label: str) -> str:
    return (
        f"PROJECT: {project_name}\n"
        f"WEEK OF: {week_label}\n\n"
        f"{jm.SUMMARY_HEADING}\n"
        "(Accomplishments narrative will appear after your first entry this week.)\n\n"
        f"{HOURS_START}\n"
        "(Hours summary syncs from the Google Sheet Dashboard after each log entry.)\n\n"
        f"{CHART_START}\n"
        "(Actual vs Estimate chart syncs from the Sheet Dashboard.)\n\n"
        f"{ACTIVITY_START}\n"
        "(Detailed Activity Log lives in Google Sheets and syncs here after each "
        "Slack submission.)\n"
    )


def initialize_document_structure(
    document_id: str,
    project_name: str,
    week_label: str,
    service=None,
) -> bool:
    service = service or get_docs_service()
    document = get_document(service, document_id)
    body_content = _collect_body_elements(document)

    if not body_content:
        print("[JOURNAL ERROR] Document body is empty or unreadable.")
        return False

    existing_text = read_document_text(document_id, service=service)
    has_core_markers = (
        jm.SUMMARY_HEADING in existing_text
        and HOURS_START in existing_text
        and ACTIVITY_START in existing_text
    )
    if has_core_markers and CHART_START in existing_text:
        ensure_document_heading_styles(document_id, service=service)
        apply_document_heading_styles(document_id, service=service)
        return True
    if has_core_markers and CHART_START not in existing_text:
        ok = _ensure_chart_markers(document_id, service=service)
        ensure_document_heading_styles(document_id, service=service)
        apply_document_heading_styles(document_id, service=service)
        return ok

    end_index = body_content[-1].get("endIndex", 1) - 1
    replacement = _build_doc_header(project_name, week_label)
    # Preserve prior document body under a legacy notice once.
    leftover = existing_text.strip()
    if leftover:
        replacement += f"\n{jm.LEGACY_HEADING}\n{leftover}\n"

    requests = []
    # Empty Docs are typically just a single newline (start=1, end=1 after -1).
    # deleteContentRange rejects empty ranges, so skip delete in that case.
    if end_index > 1:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index}
                }
            }
        )
    requests.append(
        {"insertText": {"location": {"index": 1}, "text": replacement}}
    )
    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()
    ensure_document_heading_styles(document_id, service=service)
    apply_document_heading_styles(document_id, service=service)
    print(f"  [JOURNAL] Initialized document structure for {document_id}")
    return True


def _ensure_chart_markers(document_id: str, service=None) -> bool:
    """Insert Category Chart heading before Detailed Activity Log if missing."""
    service = service or get_docs_service()
    document = get_document(service, document_id)
    body_content = _collect_body_elements(document)
    activity_start = _find_marker_indices(body_content, ACTIVITY_START)
    if not activity_start:
        print("[JOURNAL WARNING] Could not insert chart section — activity heading missing.")
        return False
    activity_idx, _ = activity_start
    insert_text = (
        f"{CHART_START}\n"
        "(Actual vs Estimate chart syncs from the Sheet Dashboard.)\n\n"
    )
    service.documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {
                    "insertText": {
                        "location": {"index": activity_idx},
                        "text": insert_text,
                    }
                }
            ]
        },
    ).execute()
    print("  [JOURNAL] Inserted Category Chart section into document.")
    return True


def _force_normal_text_range(
    document_id: str,
    start_index: int,
    end_index: int,
    service=None,
) -> None:
    """
    Apply Google Docs' built-in "Normal text" named style (API: NORMAL_TEXT).

    Text inserted after a Heading 1 paragraph inherits HEADING_1; this resets
    those paragraphs to Normal text. Direct run formatting (bold/size/color)
    inherited from H1 is cleared so the Normal text style can show through.
    """
    if start_index is None or end_index is None or start_index >= end_index:
        return
    service = service or get_docs_service()
    service.documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {
                    "updateParagraphStyle": {
                        "range": {
                            "startIndex": start_index,
                            "endIndex": end_index,
                        },
                        # Docs UI: Styles → Normal text
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        "fields": "namedStyleType",
                    }
                },
                {
                    # Clear direct formatting so the Normal text named style applies.
                    "updateTextStyle": {
                        "range": {
                            "startIndex": start_index,
                            "endIndex": end_index,
                        },
                        "textStyle": {},
                        "fields": "bold,italic,fontSize,foregroundColor",
                    }
                },
            ]
        },
    ).execute()


def replace_summary_section(
    document_id: str,
    summary_body: str,
    service=None,
) -> bool:
    service = service or get_docs_service()
    document = get_document(service, document_id)
    body_content = _collect_body_elements(document)

    summary_range = _find_marker_indices(body_content, jm.SUMMARY_HEADING)
    hours_range = _find_marker_indices(body_content, HOURS_START)
    if not summary_range or not hours_range:
        print("[JOURNAL ERROR] Could not locate summary/hours section markers.")
        return False

    _, summary_end = summary_range
    hours_start, _ = hours_range
    if summary_end > hours_start:
        print("[JOURNAL ERROR] Invalid summary section range.")
        return False

    requests = []
    if summary_end < hours_start:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {"startIndex": summary_end, "endIndex": hours_start}
                }
            }
        )
    insert_text = f"\n{summary_body}\n"
    requests.append(
        {
            "insertText": {
                "location": {"index": summary_end},
                "text": insert_text,
            }
        }
    )
    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()

    # Inserted copy inherits Heading 1 from "Weekly Summary" — force Normal Text.
    inserted_end = summary_end + len(insert_text)
    _force_normal_text_range(document_id, summary_end, inserted_end, service=service)
    return True


def _section_end_index(body_content: list[dict], until_markers: list[str]) -> int | None:
    """
    Index where the next section starts (content before this index is this section's
    body). If none of until_markers exist, use the end of the document body.
    """
    for marker in until_markers:
        found = _find_marker_indices(body_content, marker)
        if found:
            return found[0]
    if not body_content:
        return None
    return body_content[-1].get("endIndex", 1) - 1


def replace_section_body(
    document_id: str,
    start_marker: str,
    body_text: str,
    until_markers: list[str],
    service=None,
) -> bool:
    """
    Replace content after start_marker until the first found until_marker
    (next section heading). Markers themselves are kept. If no until_marker
    is present, replaces through the end of the document body.
    """
    service = service or get_docs_service()
    document = get_document(service, document_id)
    body_content = _collect_body_elements(document)

    start_range = _find_marker_indices(body_content, start_marker)
    if not start_range:
        print(f"[JOURNAL ERROR] Missing section start marker: {start_marker!r}")
        return False

    _, start_end = start_range
    end_start = _section_end_index(body_content, until_markers)
    if end_start is None or start_end > end_start:
        print(f"[JOURNAL ERROR] Invalid section range for {start_marker!r}.")
        return False

    text = body_text if body_text.endswith("\n") else body_text + "\n"
    insert_text = f"\n{text}"
    requests = []
    if start_end < end_start:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {"startIndex": start_end, "endIndex": end_start}
                }
            }
        )
    requests.append(
        {
            "insertText": {
                "location": {"index": start_end},
                "text": insert_text,
            }
        }
    )
    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()

    # Body under an H1 section title must not inherit Heading 1.
    inserted_end = start_end + len(insert_text)
    _force_normal_text_range(document_id, start_end, inserted_end, service=service)
    return True


def _get_drive_service():
    credentials, _ = google.auth.default(scopes=SCOPES)
    return build("drive", "v3", credentials=credentials)


def _folder_id_for_spreadsheet(spreadsheet_id: str) -> str:
    drive = _get_drive_service()
    meta = (
        drive.files()
        .get(
            fileId=spreadsheet_id,
            fields="parents",
            supportsAllDrives=True,
        )
        .execute()
    )
    parents = meta.get("parents") or []
    return parents[0] if parents else ""


def _upload_temp_public_png(
    png_bytes: bytes,
    filename: str,
    folder_id: str = "",
) -> tuple[str, str]:
    """
    Upload PNG to Drive with anyone-with-link read; return (file_id, public_uri).

    If a file with the same name already exists in the folder, replace its
    contents in place instead of creating a duplicate.
    """
    drive = _get_drive_service()
    media = MediaIoBaseUpload(io.BytesIO(png_bytes), mimetype="image/png", resumable=False)
    file_id = ""

    if folder_id:
        # Prefer oldest match; trash any other same-name copies.
        matches = project_router._list_files_in_folder(
            folder_id, filename, "image/png", drive=drive
        )
        if matches:
            file_id = matches[0]["id"]
            for extra in matches[1:]:
                print(
                    f"  [JOURNAL] Trashing duplicate chart PNG {extra['id']} "
                    f"(keeping {file_id})",
                    flush=True,
                )
                _trash_drive_file(extra["id"])

    if file_id:
        drive.files().update(
            fileId=file_id,
            media_body=media,
            supportsAllDrives=True,
            fields="id",
        ).execute()
        print(f"  [JOURNAL] Replaced existing chart PNG '{filename}' ({file_id})", flush=True)
    else:
        body: dict = {"name": filename, "mimeType": "image/png"}
        if folder_id:
            body["parents"] = [folder_id]
        created = (
            drive.files()
            .create(
                body=body,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = created["id"]
        print(f"  [JOURNAL] Uploaded chart PNG '{filename}' ({file_id})", flush=True)

    try:
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()
    except Exception as exc:
        # Permission may already exist from a prior upload.
        print(f"  [JOURNAL] Chart PNG share note: {exc}", flush=True)

    uri = f"https://drive.google.com/uc?export=download&id={file_id}"
    return file_id, uri


def _trash_drive_file(file_id: str) -> None:
    try:
        drive = _get_drive_service()
        drive.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()
    except Exception as exc:
        print(f"  [JOURNAL WARNING] Could not trash temp chart file {file_id}: {exc}")


def compile_task_budget_outlook(
    project_name: str,
    *,
    start_date: str,
    estimated_end_date: str,
    as_of_date: str,
    remaining_weeks: int | None,
    tasks: list[dict],
) -> str:
    """
    Ask Gemini for a succinct 2-3 sentence budget projection based on
    Firestore project dates and each task's completed / estimated / avg weekly spend.
    """
    fallback = (
        "Budget outlook: set Project Estimated End Date and each task's "
        "Estimated $ on the Dashboard to enable on-budget projections. "
        "Avg Weekly Spend is calculated from Completed $."
    )
    if not tasks:
        return fallback

    remaining_weeks_text = (
        str(remaining_weeks)
        if remaining_weeks is not None
        else "(unknown — estimated_end_date missing)"
    )
    system_instruction = """
You are a concise project controls analyst for an architecture/engineering firm.
Write exactly 2-3 short sentences (no bullets, no headings) assessing whether
each task is projected to finish on budget by the project's estimated end date.

Authoritative inputs are provided in the user message (from Firestore). Treat them
as complete — do not ask for a current date, and do not say a current date is
needed when as_of_date and/or remaining_weeks are provided.

Use only the numbers provided:
- remaining_budget = estimated - completed
- remaining_weeks is already computed from as_of_date → estimated_end_date
- If avg_weekly_spend > 0 and remaining_weeks is known, compare
  remaining_weeks vs remaining_budget / avg_weekly_spend
- Say clearly which tasks look on track vs at risk of overrunning
- If estimated_end_date or avg weekly spend are missing/zero, say projections
  are limited — but never invent a missing "current date" complaint
- Do not discuss the duration needed to complete the project; billable money
  does not equate to completeness of the work
Do not invent numbers. Keep total length under 80 words.
"""
    user_prompt = f"""
Project: {project_name}
Project start date (Firestore): {start_date or "(not set)"}
Project estimated end date (Firestore): {estimated_end_date or "(not set)"}
As-of date (today): {as_of_date or "(not set)"}
Remaining weeks until estimated end: {remaining_weeks_text}
Tasks JSON (Firestore):
{json.dumps(tasks, indent=2)}
"""
    try:
        ai_client = _get_genai_client()
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
            ),
        )
        text = (getattr(response, "text", None) or "").strip()
        if text:
            return text
    except Exception as exc:
        print(f"  [JOURNAL WARNING] Budget outlook generation failed: {exc}")
    return fallback


def _remaining_weeks_until(end_iso: str, as_of_iso: str) -> int | None:
    """Whole weeks from as_of through estimated_end_date; None if end is blank."""
    from datetime import date as date_cls

    end_iso = (end_iso or "").strip()
    as_of_iso = (as_of_iso or "").strip()
    if not end_iso or not as_of_iso:
        return None
    try:
        end_d = date_cls.fromisoformat(end_iso[:10])
        as_of_d = date_cls.fromisoformat(as_of_iso[:10])
    except ValueError:
        return None
    days = (end_d - as_of_d).days
    if days < 0:
        return 0
    return days // 7


def sync_category_chart_into_doc(
    document_id: str,
    spreadsheet_id: str,
    service=None,
    project_key: str = "",
    project_name: str = "",
) -> bool:
    """
    Rebuild task budget progress chart + Gemini budget outlook between CHART markers.

    Outlook inputs (dates + task money fields) come primarily from Firestore.
    """
    service = service or get_docs_service()
    if CHART_START not in read_document_text(document_id, service=service):
        _ensure_chart_markers(document_id, service=service)

    sheets = agent_sheets.get_sheets_service()
    # Dashboard was already rebuilt when ActivityLog was appended — do not
    # refresh again (Sheets read quota is 60/min/user).

    # Sheet rows only for chart fallback if Firestore has no tasks yet.
    sheet_task_rows = agent_sheets.get_dashboard_task_rows(
        spreadsheet_id, sheets=sheets
    )

    start_date = ""
    end_date = ""
    tasks_payload: list[dict] = []
    task_rows: list[tuple[str, float, float]] = []
    try:
        import firestore_store

        key = (project_key or "").strip()
        if not key:
            key = agent_sheets._read_stored_project_key(sheets, spreadsheet_id)
        if key:
            proj = firestore_store.get_project(key)
            if proj:
                start_date = (proj.start_date or "").strip()
                end_date = (proj.estimated_end_date or "").strip()
                project_name = project_name or proj.name
                print(
                    f"  [JOURNAL] Outlook schedule from Firestore `{key}`: "
                    f"start={start_date or '(blank)'} end={end_date or '(blank)'}",
                    flush=True,
                )
            fs_tasks = firestore_store.list_tasks(key)
            for ft in fs_tasks:
                completed = int(ft.completed or 0)
                estimated = int(ft.estimated or 0)
                avg_weekly = int(ft.avg_weekly_spend or 0)
                tasks_payload.append(
                    {
                        "task": ft.name,
                        "completed": completed,
                        "estimated": estimated,
                        "avg_weekly_spend": avg_weekly,
                        "remaining": max(0, estimated - completed),
                    }
                )
                task_rows.append((ft.name, float(completed), float(estimated)))
    except Exception as exc:
        print(f"  [JOURNAL WARNING] Could not load Firestore outlook data: {exc}")

    # Fallback to sheet values only when Firestore had no task rows.
    if not tasks_payload and sheet_task_rows:
        for label, completed, estimated in sheet_task_rows:
            if label in {"(no tasks yet)", ""}:
                continue
            c = int(round(completed))
            e = int(round(estimated))
            tasks_payload.append(
                {
                    "task": label,
                    "completed": c,
                    "estimated": e,
                    "avg_weekly_spend": 0,
                    "remaining": max(0, e - c),
                }
            )
            task_rows.append((label, float(c), float(e)))

    if not task_rows:
        task_rows = [("(no tasks yet)", 0.0, 0.0)]

    as_of_date = datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE)).date().isoformat()
    remaining_weeks = _remaining_weeks_until(end_date, as_of_date)

    outlook = compile_task_budget_outlook(
        project_name or "Project",
        start_date=start_date,
        estimated_end_date=end_date,
        as_of_date=as_of_date,
        remaining_weeks=remaining_weeks,
        tasks=tasks_payload,
    )

    png_bytes = agent_sheets.render_task_progress_chart_png(task_rows)
    file_id = ""
    try:
        folder_id = _folder_id_for_spreadsheet(spreadsheet_id)
        file_id, uri = _upload_temp_public_png(
            png_bytes,
            f"prdei-category-chart-{spreadsheet_id[:8]}.png",
            folder_id=folder_id,
        )

        document = get_document(service, document_id)
        body_content = _collect_body_elements(document)
        start_range = _find_marker_indices(body_content, CHART_START)
        end_start = _section_end_index(body_content, [ACTIVITY_START, jm.LEGACY_HEADING])
        if not start_range or end_start is None:
            print("[JOURNAL ERROR] Chart section bounds missing after ensure.")
            return False

        _, start_end = start_range
        if start_end > end_start:
            print("[JOURNAL ERROR] Invalid chart section range.")
            return False
        requests = []
        if start_end < end_start:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {"startIndex": start_end, "endIndex": end_start}
                    }
                }
            )
        # Outlook text, blank line, then image placeholder.
        intro = f"{outlook.strip()}\n\n \n"
        requests.append(
            {
                "insertText": {
                    "location": {"index": start_end},
                    "text": intro,
                }
            }
        )
        image_index = start_end + len(intro) - 2  # on the space before final newline
        requests.append(
            {
                "insertInlineImage": {
                    "location": {"index": image_index},
                    "uri": uri,
                    "objectSize": {
                        "height": {"magnitude": 280, "unit": "PT"},
                        "width": {"magnitude": 480, "unit": "PT"},
                    },
                }
            }
        )

        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

        # Outlook inherits Heading 1 from "Category Chart" — force Normal Text.
        outlook_end = start_end + len(outlook.strip()) + 2  # outlook + "\n\n"
        _force_normal_text_range(
            document_id, start_end, outlook_end, service=service
        )
        print("  [JOURNAL] Synced budget outlook + task chart into Doc.")
        return True
    except Exception as exc:
        print(f"  [JOURNAL ERROR] Chart image sync failed: {exc}")
        return False
    finally:
        if file_id:
            _trash_drive_file(file_id)


def sync_sheet_sections_into_doc(
    document_id: str,
    spreadsheet_id: str,
    project_name: str,
    service=None,
    project_key: str = "",
) -> bool:
    """
    Copy Hours + Activity Log from Sheets into the Google Doc marker sections,
    and re-embed the task budget progress chart below Hours by Task.
    """
    service = service or get_docs_service()
    week_start, week_end, week_label = jm.get_current_week_range()
    entries = agent_sheets.read_week_entries(
        spreadsheet_id, week_start=week_start, week_end=week_end
    )
    total_hours, _hours_by_category = agent_sheets.get_dashboard_totals(spreadsheet_id)
    task_rows = agent_sheets.get_dashboard_task_rows(spreadsheet_id)
    hours_by_task = jm.compute_hours_by_task(entries)
    if not total_hours:
        total_hours = round(sum(entry.hours for entry in entries), 2)

    estimates = {label: estimate for label, _completed, estimate in task_rows}
    completed_by_task = {
        label: completed for label, completed, _estimate in task_rows
    }

    hours_lines = [
        f"Week of: {week_label}",
        f"Total Hours: {total_hours:g}",
        "",
        "Hours by Task:",
    ]
    if hours_by_task or estimates or completed_by_task or task_rows:
        # Prefer Dashboard task order; include any tasks logged this week.
        labels = [r[0] for r in task_rows] if task_rows else list(hours_by_task.keys())
        for name in hours_by_task:
            if name not in labels:
                labels.append(name)
        if not labels:
            hours_lines.append("(none this week)")
        else:
            for task_name in labels:
                if task_name == "(no tasks yet)":
                    continue
                actual = hours_by_task.get(task_name, 0.0)
                estimate = estimates.get(task_name, 0.0)
                completed = completed_by_task.get(task_name, 0.0)
                # Task name alone so it can receive bold Normal styling.
                hours_lines.append(task_name)
                if estimate:
                    hours_lines.append(
                        f"Actual {actual:g} hrs | Completed ${completed:g} | Estimated ${estimate:g}"
                    )
                else:
                    hours_lines.append(
                        f"Actual {actual:g} hrs | Completed ${completed:g} | Estimated $ (enter in Sheet)"
                    )
                hours_lines.append("")
    else:
        hours_lines.append("(none this week)")

    activity_lines = ["Timestamp | User | Hours | Task | Category | Activity"]
    for entry in entries:
        activity_lines.append(
            f"{entry.timestamp_str} | {entry.user} | {entry.hours:g} | "
            f"{entry.task_label or '-'} | {entry.category_label} | {entry.activity}"
        )
    if len(activity_lines) == 1:
        activity_lines.append("(no entries this week)")

    hours_ok = replace_section_body(
        document_id,
        HOURS_START,
        "\n".join(hours_lines),
        until_markers=[CHART_START, ACTIVITY_START, jm.LEGACY_HEADING],
        service=service,
    )
    chart_ok = sync_category_chart_into_doc(
        document_id,
        spreadsheet_id,
        service=service,
        project_key=project_key,
        project_name=project_name,
    )
    activity_ok = replace_section_body(
        document_id,
        ACTIVITY_START,
        "\n".join(activity_lines),
        until_markers=[jm.LEGACY_HEADING],
        service=service,
    )
    print(
        f"  [JOURNAL] Synced Sheet tables into Doc "
        f"(hours={hours_ok}, chart={chart_ok}, activity={activity_ok}, "
        f"entries={len(entries)})"
    )
    ensure_document_heading_styles(document_id, service=service)
    h1_n, bold_n = apply_document_heading_styles(document_id, service=service)
    if h1_n or bold_n:
        print(f"  [JOURNAL] Applied styles (H1={h1_n}, bold Normal={bold_n}).")
    return hours_ok and activity_ok


def _get_genai_client():
    os.environ["GOOGLE_CLOUD_QUOTA_PROJECT"] = PROJECT_ID
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def compile_weekly_summary(
    entries: list[jm.LogEntry],
    project_name: str,
    week_label: str,
    total_hours: float | None = None,
    hours_by_category: dict[str, float] | None = None,
    hours_by_task: dict[str, float] | None = None,
) -> Optional[dict]:
    if not entries:
        return None

    if total_hours is None:
        total_hours = round(sum(entry.hours for entry in entries), 2)
    if hours_by_task is None:
        hours_by_task = jm.compute_hours_by_task(entries)
    if hours_by_category is None:
        hours_by_category = jm.compute_hours_by_category(entries)

    entries_payload = [entry.to_dict() for entry in entries]

    system_instruction = """
You are an expert technical writer for an architecture and engineering firm.
Compile weekly project journal summaries for client review.

Rules:
1. Use a formal, objective, technical tone.
2. Summarize only work described in the supplied log entries.
3. Do NOT invent hours, activities, dates, or names.
4. The accomplishments_narrative should be 2-4 paragraphs covering key themes.
5. Do not restate the hours tables — those live in Google Sheets.
"""

    user_prompt = f"""
Project: {project_name}
Week: {week_label}
Total hours (verified): {total_hours}
Hours by task (verified): {json.dumps(hours_by_task)}
Hours by category (verified): {json.dumps(hours_by_category)}

Log entries (JSON):
{json.dumps(entries_payload, indent=2)}

Return JSON with exactly these fields:
- accomplishments_narrative (string)
"""

    try:
        ai_client = _get_genai_client()
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        compiled = json.loads(response.text.strip())
    except Exception as exc:
        print(f"  [AI ERROR] Gemini weekly compilation failed: {exc}")
        print("  [AI FALLBACK] Using Python-generated summary instead.")
        compiled = jm.build_fallback_summary(entries)

    narrative = (compiled or {}).get("accomplishments_narrative") or (
        "Work was logged this week. See the detailed activity log in Google Sheets "
        "(and the synced table below)."
    )
    return {
        "total_hours": total_hours,
        "hours_by_category": hours_by_category,
        "accomplishments_narrative": narrative,
    }


def render_doc_narrative(accomplishments_narrative: str) -> str:
    narrative = (accomplishments_narrative or "").strip()
    return (
        "Accomplishments\n"
        f"{narrative}\n\n"
        "_Total Hours and Hours by Task are maintained in the linked Google Sheet "
        "Dashboard and synced into the Hours Summary section below._\n"
    )


def _update_summary_from_sheet(
    document_id: str,
    spreadsheet_id: str,
    project_name: str,
    service,
    project_key: str = "",
) -> bool:
    week_start, week_end, week_label = jm.get_current_week_range()
    # Week cells only — ActivityLog append already refreshed Dashboard tables.
    agent_sheets.update_dashboard_week(
        spreadsheet_id, week_start, week_label, project_key=project_key, refresh=False
    )

    week_entries = agent_sheets.read_week_entries(
        spreadsheet_id, week_start=week_start, week_end=week_end
    )
    if not week_entries:
        print("  [JOURNAL WARNING] No Sheet entries found for current week.")
        return False

    total_hours, _hours_by_category = agent_sheets.get_dashboard_totals(spreadsheet_id)
    if not total_hours:
        total_hours = round(sum(entry.hours for entry in week_entries), 2)
    hours_by_task = jm.compute_hours_by_task(week_entries)
    hours_by_category = jm.compute_hours_by_category(week_entries)

    compiled = compile_weekly_summary(
        week_entries,
        project_name,
        week_label,
        total_hours=total_hours,
        hours_by_category=hours_by_category,
        hours_by_task=hours_by_task,
    )
    if not compiled:
        return False

    summary_body = render_doc_narrative(compiled["accomplishments_narrative"])
    if not replace_summary_section(document_id, summary_body, service=service):
        return False
    return sync_sheet_sections_into_doc(
        document_id,
        spreadsheet_id,
        project_name,
        service=service,
        project_key=project_key,
    )


def refresh_weekly_summary(
    document_id: str,
    project_name: str,
    project_key: str = "",
    spreadsheet_id: str = "",
) -> JournalUpdateResult:
    if not document_id:
        return JournalUpdateResult(success=False, error_message="Document ID is empty.")

    try:
        service = get_docs_service()
        _, _, week_label = jm.get_current_week_range()
        initialize_document_structure(document_id, project_name, week_label, service=service)

        if not spreadsheet_id and project_key:
            assets = project_router.ensure_project_assets(project_key)
            spreadsheet_id = assets.spreadsheet_id if assets else ""
        if not spreadsheet_id:
            return JournalUpdateResult(
                success=False,
                error_message="No Detailed Activity Log spreadsheet found/created.",
            )

        spreadsheet_id = agent_sheets.ensure_spreadsheet(
            project_name, spreadsheet_id, project_key=project_key
        )
        # One Dashboard rebuild for /refreshjournal (ensure_spreadsheet no longer does this).
        agent_sheets.refresh_dashboard_tables(
            agent_sheets.get_sheets_service(),
            spreadsheet_id,
            project_key=project_key,
            autosize=False,
        )
        summary_updated = _update_summary_from_sheet(
            document_id, spreadsheet_id, project_name, service, project_key=project_key
        )
        # Optional Apps Script webhook for native linked charts (if configured).
        agent_sheets.trigger_docs_refresh(document_id, spreadsheet_id, project_name)
        return JournalUpdateResult(
            success=summary_updated,
            summary_updated=summary_updated,
            docs_refreshed=summary_updated,
            spreadsheet_id=spreadsheet_id,
            error_message="" if summary_updated else "Could not refresh weekly summary.",
        )
    except Exception as exc:
        print(f"  [JOURNAL ERROR] Failed to refresh summary: {exc}")
        return JournalUpdateResult(success=False, error_message=str(exc))


def process_journal_update(
    document_id: str,
    project_name: str,
    new_entries: list[jm.LogEntry],
    project_key: str = "",
    spreadsheet_id: str = "",
    rate: int = 0,
) -> JournalUpdateResult:
    if not document_id:
        return JournalUpdateResult(success=False, error_message="Document ID is empty.")
    if not new_entries:
        return JournalUpdateResult(success=False, error_message="No entries to log.")

    hours_logged = round(sum(entry.hours for entry in new_entries), 2)

    try:
        service = get_docs_service()
        week_start, week_end, week_label = jm.get_current_week_range()

        if not spreadsheet_id and project_key:
            assets = project_router.ensure_project_assets(project_key)
            if assets:
                document_id = document_id or assets.document_id
                spreadsheet_id = assets.spreadsheet_id
        if not spreadsheet_id:
            return JournalUpdateResult(
                success=False,
                error_message="No Detailed Activity Log spreadsheet found/created.",
            )

        # First Slack entry: default project schedule if still blank.
        if project_key:
            try:
                import firestore_store

                firestore_store.ensure_project_schedule_on_first_log(
                    project_key,
                    entry_when=new_entries[0].timestamp,
                )
            except Exception as exc:
                print(
                    f"  [JOURNAL WARNING] Could not set project schedule defaults: {exc}",
                    flush=True,
                )

        spreadsheet_id = agent_sheets.ensure_spreadsheet(
            project_name, spreadsheet_id, project_key=project_key
        )
        agent_sheets.update_dashboard_week(
            spreadsheet_id, week_start, week_label, project_key=project_key
        )
        agent_sheets.append_log_entries(
            spreadsheet_id,
            new_entries,
            rate=rate,
            project_key=project_key,
        )
        log_appended = True

        if not initialize_document_structure(
            document_id, project_name, week_label, service=service
        ):
            return JournalUpdateResult(
                success=False,
                log_appended=True,
                spreadsheet_id=spreadsheet_id,
                error_message="Logged to Sheet but could not initialize document structure.",
            )

        summary_updated = _update_summary_from_sheet(
            document_id, spreadsheet_id, project_name, service, project_key=project_key
        )
        if not summary_updated:
            print("  [JOURNAL WARNING] Sheet saved but Doc narrative/table sync failed.")

        # Optional extra step for native linked charts if webapp URL is configured.
        agent_sheets.trigger_docs_refresh(document_id, spreadsheet_id, project_name)

        print(
            f"  [JOURNAL SUCCESS] Logged {hours_logged:g} hr(s) to Sheet {spreadsheet_id}; "
            f"summary_updated={summary_updated}"
        )
        return JournalUpdateResult(
            success=log_appended,
            log_appended=log_appended,
            summary_updated=summary_updated,
            docs_refreshed=summary_updated,
            hours_logged=hours_logged,
            spreadsheet_id=spreadsheet_id,
        )

    except HttpError as http_err:
        print(
            f"  [JOURNAL API ERROR] Google API returned status "
            f"{http_err.resp.status}: {http_err._get_reason()}"
        )
        print(
            "  -> Ensure your Cloud Run Service Account email has Editor access "
            "on the project Drive folder (create files + edit Doc/Sheet)."
        )
        return JournalUpdateResult(
            success=False,
            error_message=f"Google API error: {http_err._get_reason()}",
        )
    except Exception as exc:
        print(f"  [JOURNAL ERROR] Failed to update journal: {exc}")
        return JournalUpdateResult(success=False, error_message=str(exc))


def append_to_journal(
    document_id: str,
    user_name: str,
    task_category: str,
    raw_details: str,
) -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    entry = jm.LogEntry(
        timestamp=datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE)),
        user=user_name,
        hours=1.0,
        category=task_category,
        activity=raw_details,
    )
    result = process_journal_update(document_id, "Project Journal", [entry])
    return result.success and result.log_appended
