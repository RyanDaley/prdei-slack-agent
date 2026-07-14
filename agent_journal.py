"""
AEC Agent Journal Integration.

Architecture:
  - Google Sheets = source of truth for Detailed Activity Log, Total Hours,
    and Hours by Category (Dashboard formulas).
  - Google Docs = client narrative (Gemini) plus tables rebuilt from Sheets
    by Apps Script (native Docs "Update all" on linked charts is not exposed
    to Apps Script / Docs API).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

import google.auth
from google import genai
from google.genai import types as genai_types
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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

HOURS_START = "--- HOURS SUMMARY (FROM SHEETS) ---"
HOURS_END = "--- END HOURS SUMMARY ---"
ACTIVITY_START = "--- DETAILED ACTIVITY LOG ---"
ACTIVITY_END = "--- END DETAILED ACTIVITY LOG ---"


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
        "(Hours tables sync from the Google Sheet — insert a linked chart/table from "
        f"the '{agent_sheets.spreadsheet_title_for_project(project_name)}' Dashboard "
        "tab for live visuals, or rely on the Apps Script sync below.)\n"
        f"{HOURS_END}\n\n"
        f"{ACTIVITY_START}\n"
        "(Detailed Activity Log lives in Google Sheets. Linked tables/charts from the "
        "ActivityLog tab can be pasted here; Apps Script rebuilds the synced table "
        "after each Slack submission.)\n"
        f"{ACTIVITY_END}\n"
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
    has_markers = (
        jm.SUMMARY_HEADING in existing_text
        and HOURS_START in existing_text
        and ACTIVITY_START in existing_text
    )
    if has_markers:
        return True

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
    print(f"  [JOURNAL] Initialized document structure for {document_id}")
    return True


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
    if summary_end >= hours_start:
        print("[JOURNAL ERROR] Invalid summary section range.")
        return False

    requests = [
        {
            "deleteContentRange": {
                "range": {"startIndex": summary_end, "endIndex": hours_start}
            }
        },
        {
            "insertText": {
                "location": {"index": summary_end},
                "text": f"\n{summary_body}\n",
            }
        },
    ]
    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()
    return True


def _get_genai_client():
    os.environ["GOOGLE_CLOUD_QUOTA_PROJECT"] = PROJECT_ID
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def compile_weekly_summary(
    entries: list[jm.LogEntry],
    project_name: str,
    week_label: str,
    total_hours: float | None = None,
    hours_by_category: dict[str, float] | None = None,
) -> Optional[dict]:
    if not entries:
        return None

    if total_hours is None:
        total_hours = round(sum(entry.hours for entry in entries), 2)
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


def render_doc_narrative(week_label: str, accomplishments_narrative: str) -> str:
    narrative = (accomplishments_narrative or "").strip()
    return (
        f"Week of {week_label}\n\n"
        "Accomplishments:\n"
        f"{narrative}\n\n"
        "_Total Hours and Hours by Category are maintained in the linked Google Sheet "
        "Dashboard and synced into the Hours Summary section below._\n"
    )


def _update_summary_from_sheet(
    document_id: str,
    spreadsheet_id: str,
    project_name: str,
    service,
) -> bool:
    week_start, week_end, week_label = jm.get_current_week_range()
    agent_sheets.update_dashboard_week(spreadsheet_id, week_start, week_label)

    week_entries = agent_sheets.read_week_entries(
        spreadsheet_id, week_start=week_start, week_end=week_end
    )
    if not week_entries:
        print("  [JOURNAL WARNING] No Sheet entries found for current week.")
        return False

    total_hours, hours_by_category = agent_sheets.get_dashboard_totals(spreadsheet_id)
    if not hours_by_category:
        hours_by_category = jm.compute_hours_by_category(week_entries)
    if not total_hours:
        total_hours = round(sum(entry.hours for entry in week_entries), 2)

    compiled = compile_weekly_summary(
        week_entries,
        project_name,
        week_label,
        total_hours=total_hours,
        hours_by_category=hours_by_category,
    )
    if not compiled:
        return False

    summary_body = render_doc_narrative(week_label, compiled["accomplishments_narrative"])
    return replace_summary_section(document_id, summary_body, service=service)


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

        spreadsheet_id = agent_sheets.ensure_spreadsheet(project_name, spreadsheet_id)
        summary_updated = _update_summary_from_sheet(
            document_id, spreadsheet_id, project_name, service
        )
        docs_refreshed = agent_sheets.trigger_docs_refresh(
            document_id, spreadsheet_id, project_name
        )
        return JournalUpdateResult(
            success=summary_updated,
            summary_updated=summary_updated,
            docs_refreshed=docs_refreshed,
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

        spreadsheet_id = agent_sheets.ensure_spreadsheet(project_name, spreadsheet_id)
        agent_sheets.update_dashboard_week(spreadsheet_id, week_start, week_label)
        agent_sheets.append_log_entries(spreadsheet_id, new_entries)
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
            document_id, spreadsheet_id, project_name, service
        )
        if not summary_updated:
            print("  [JOURNAL WARNING] Sheet saved but Doc narrative update failed.")

        docs_refreshed = agent_sheets.trigger_docs_refresh(
            document_id, spreadsheet_id, project_name
        )

        print(
            f"  [JOURNAL SUCCESS] Logged {hours_logged:g} hr(s) to Sheet {spreadsheet_id}; "
            f"summary_updated={summary_updated}; docs_refreshed={docs_refreshed}"
        )
        return JournalUpdateResult(
            success=log_appended,
            log_appended=log_appended,
            summary_updated=summary_updated,
            docs_refreshed=docs_refreshed,
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
