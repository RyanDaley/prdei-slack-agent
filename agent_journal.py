"""
AEC Agent Journal Integration.
Reads and writes Google Docs, compiles weekly summaries with Gemini,
and maintains an append-only detailed activity log.
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

import journal_models as jm

PROJECT_ID = "prdei-ai-sandbox"
LOCATION = "us-west1"
GEMINI_MODEL = "gemini-2.5-flash"
SCOPES = ["https://www.googleapis.com/auth/documents"]


@dataclass
class JournalUpdateResult:
    success: bool
    log_appended: bool = False
    summary_updated: bool = False
    hours_logged: float = 0.0
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


def parse_log_entries(
    doc_text: str,
    week_start=None,
    week_end=None,
) -> list[jm.LogEntry]:
    entries = []
    for line in doc_text.splitlines():
        parsed = jm.parse_log_line(line)
        if parsed:
            entries.append(parsed)

    if week_start is not None and week_end is not None:
        return jm.filter_entries_for_week(entries, week_start, week_end)
    return entries


def _build_doc_header(project_name: str, week_label: str) -> str:
    return (
        f"PROJECT: {project_name}\n"
        f"WEEK OF: {week_label}\n\n"
        f"{jm.SUMMARY_HEADING}\n"
        "(Summary will appear after your first entry this week.)\n\n"
        f"{jm.DETAIL_LOG_HEADING}\n"
    )


def _build_legacy_section(legacy_text: str) -> str:
    cleaned = legacy_text.strip()
    if not cleaned:
        return ""
    return f"\n{jm.LEGACY_HEADING}\n{cleaned}\n"


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
    has_markers = jm.SUMMARY_HEADING in existing_text and jm.DETAIL_LOG_HEADING in existing_text
    if has_markers:
        return True

    end_index = body_content[-1].get("endIndex", 1) - 1
    legacy_text = existing_text.strip()

    if legacy_text:
        replacement = _build_doc_header(project_name, week_label) + _build_legacy_section(legacy_text)
        requests = [
            {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index}}},
            {"insertText": {"location": {"index": 1}, "text": replacement}},
        ]
    else:
        replacement = _build_doc_header(project_name, week_label)
        requests = [{"insertText": {"location": {"index": end_index}, "text": replacement}}]

    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()
    print(f"  [JOURNAL] Initialized document structure for {document_id}")
    return True


def append_log_entries(
    document_id: str,
    entries: list[jm.LogEntry],
    service=None,
) -> bool:
    if not entries:
        return True

    service = service or get_docs_service()
    text = "".join(entry.to_log_line() for entry in entries)
    requests = [
        {
            "insertText": {
                "endOfSegmentLocation": {"segmentId": ""},
                "text": text,
            }
        }
    ]
    service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests},
    ).execute()
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
    detail_range = _find_marker_indices(body_content, jm.DETAIL_LOG_HEADING)
    if not summary_range or not detail_range:
        print("[JOURNAL ERROR] Could not locate summary/detail section markers.")
        return False

    _, summary_end = summary_range
    detail_start, _ = detail_range
    if summary_end >= detail_start:
        print("[JOURNAL ERROR] Invalid summary section range.")
        return False

    requests = [
        {
            "deleteContentRange": {
                "range": {"startIndex": summary_end, "endIndex": detail_start}
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
) -> Optional[dict]:
    if not entries:
        return None

    total_hours = round(sum(entry.hours for entry in entries), 2)
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
5. hours_by_category keys must match the category labels from the input entries.
6. total_hours must equal the sum of entry hours.
"""

    user_prompt = f"""
Project: {project_name}
Week: {week_label}
Total hours (verified): {total_hours}
Hours by category (verified): {json.dumps(hours_by_category)}

Log entries (JSON):
{json.dumps(entries_payload, indent=2)}

Return JSON with exactly these fields:
- total_hours (number)
- hours_by_category (object mapping category label to hours number)
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
        return None

    compiled["total_hours"] = total_hours
    compiled["hours_by_category"] = hours_by_category
    if not compiled.get("accomplishments_narrative"):
        compiled["accomplishments_narrative"] = (
            "Work was logged this week. See the detailed activity log below."
        )
    return compiled


def process_journal_update(
    document_id: str,
    project_name: str,
    new_entries: list[jm.LogEntry],
) -> JournalUpdateResult:
    if not document_id:
        return JournalUpdateResult(success=False, error_message="Document ID is empty.")
    if not new_entries:
        return JournalUpdateResult(success=False, error_message="No entries to log.")

    hours_logged = round(sum(entry.hours for entry in new_entries), 2)

    try:
        service = get_docs_service()
        week_start, week_end, week_label = jm.get_current_week_range()

        if not initialize_document_structure(
            document_id, project_name, week_label, service=service
        ):
            return JournalUpdateResult(
                success=False,
                error_message="Could not initialize document structure.",
            )

        append_log_entries(document_id, new_entries, service=service)
        log_appended = True

        doc_text = read_document_text(document_id, service=service)
        week_entries = parse_log_entries(doc_text, week_start, week_end)
        compiled = compile_weekly_summary(week_entries, project_name, week_label)

        summary_updated = False
        if compiled:
            summary_body = jm.render_summary_body(
                compiled["total_hours"],
                compiled["hours_by_category"],
                compiled["accomplishments_narrative"],
            )
            summary_updated = replace_summary_section(
                document_id, summary_body, service=service
            )
            if not summary_updated:
                print("  [JOURNAL WARNING] Log saved but summary section update failed.")

        print(
            f"  [JOURNAL SUCCESS] Logged {hours_logged:g} hr(s) to {document_id}; "
            f"summary_updated={summary_updated}"
        )
        return JournalUpdateResult(
            success=log_appended,
            log_appended=log_appended,
            summary_updated=summary_updated,
            hours_logged=hours_logged,
        )

    except HttpError as http_err:
        print(
            f"  [JOURNAL API ERROR] Google Docs API returned status "
            f"{http_err.resp.status}: {http_err._get_reason()}"
        )
        print(
            "  -> Ensure your Cloud Run Service Account email has "
            "'Editor' permission on the target Google Doc!"
        )
        return JournalUpdateResult(
            success=False,
            error_message=f"Google Docs API error: {http_err._get_reason()}",
        )
    except Exception as exc:
        print(f"  [JOURNAL ERROR] Failed to update journal: {exc}")
        return JournalUpdateResult(success=False, error_message=str(exc))


# Backward-compatible alias used during transition
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
