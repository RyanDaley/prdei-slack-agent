"""
AEC Agent Journal Integration.

Architecture:
  - Google Sheets = source of truth for Detailed Activity Log, Total Hours,
    Actual Hours by Category (formulas), and Estimate hours (manual).
    Dashboard includes an Actual vs Estimate bar chart.
  - Google Docs = client narrative (Gemini) plus Hours/Activity text synced
    from Sheets, with the category bar chart re-embedded as an image on each
    Activity Log update (Docs API cannot refresh native linked charts).
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from typing import Optional

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
    "Hours by Category:",
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


def _is_bold_normal_paragraph(text: str, category_labels: set[str]) -> bool:
    stripped = text.strip()
    if stripped in BOLD_NORMAL_EXACT_TITLES:
        return True
    if stripped.startswith("Total Hours:"):
        return True
    if stripped in category_labels:
        return True
    return False


def apply_document_heading_styles(document_id: str, service=None) -> tuple[int, int]:
    """
    Apply Heading 1 to section titles, and Normal text + bold to
    Accomplishments / Total Hours / category names.
    Returns (h1_count, bold_normal_count).
    """
    service = service or get_docs_service()
    document = get_document(service, document_id)
    body_content = _collect_body_elements(document)
    h1_titles = {t.strip() for t in HEADING1_TITLES}
    category_labels = set(jm.TASK_CATEGORY_LABELS.values())
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
        elif _is_bold_normal_paragraph(text, category_labels):
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
    """Upload PNG to Drive with anyone-with-link read; return (file_id, public_uri)."""
    drive = _get_drive_service()
    media = MediaIoBaseUpload(io.BytesIO(png_bytes), mimetype="image/png", resumable=False)
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
    drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()
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


def sync_category_chart_into_doc(
    document_id: str,
    spreadsheet_id: str,
    service=None,
) -> bool:
    """
    Rebuild Actual vs Estimate bar chart image between CHART markers.
    Regenerated whenever Sheets Activity Log / Dashboard data is synced.
    """
    service = service or get_docs_service()
    if CHART_START not in read_document_text(document_id, service=service):
        _ensure_chart_markers(document_id, service=service)

    sheets = agent_sheets.get_sheets_service()
    agent_sheets.ensure_category_estimate_table(sheets, spreadsheet_id)
    agent_sheets.ensure_category_bar_chart(sheets, spreadsheet_id)

    category_rows = agent_sheets.get_dashboard_category_rows(spreadsheet_id, sheets=sheets)
    if not category_rows:
        labels = list(jm.TASK_CATEGORY_LABELS.values())
        category_rows = [(label, 0.0, 0.0) for label in labels]

    png_bytes = agent_sheets.render_category_bar_chart_png(category_rows)
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
        # Placeholder newlines, then insert image between them.
        requests.append(
            {
                "insertText": {
                    "location": {"index": start_end},
                    "text": "\n \n",
                }
            }
        )
        requests.append(
            {
                "insertInlineImage": {
                    "location": {"index": start_end + 1},
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
        print("  [JOURNAL] Synced Actual vs Estimate chart image into Doc.")
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
) -> bool:
    """
    Copy Hours + Activity Log from Sheets into the Google Doc marker sections,
    and re-embed the Actual vs Estimate bar chart below Hours by Category.
    """
    service = service or get_docs_service()
    week_start, week_end, week_label = jm.get_current_week_range()
    entries = agent_sheets.read_week_entries(
        spreadsheet_id, week_start=week_start, week_end=week_end
    )
    total_hours, hours_by_category = agent_sheets.get_dashboard_totals(spreadsheet_id)
    category_rows = agent_sheets.get_dashboard_category_rows(spreadsheet_id)
    if not hours_by_category:
        hours_by_category = jm.compute_hours_by_category(entries)
    if not total_hours:
        total_hours = round(sum(entry.hours for entry in entries), 2)

    estimates = {label: estimate for label, _actual, estimate in category_rows}

    hours_lines = [
        f"Week of: {week_label}",
        f"Total Hours: {total_hours:g}",
        "",
        "Hours by Category:",
    ]
    if hours_by_category or estimates:
        labels = list(hours_by_category.keys()) or [r[0] for r in category_rows]
        # Prefer Dashboard category order when available.
        if category_rows:
            labels = [r[0] for r in category_rows]
        for category in labels:
            actual = hours_by_category.get(category, 0.0)
            estimate = estimates.get(category, 0.0)
            # Category name alone so it can receive Heading 2 styling.
            hours_lines.append(category)
            if estimate:
                hours_lines.append(
                    f"Actual {actual:g} hrs | Estimate {estimate:g} hrs"
                )
            else:
                hours_lines.append(
                    f"Actual {actual:g} hrs | Estimate (enter in Sheet)"
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
        document_id, spreadsheet_id, service=service
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


def render_doc_narrative(accomplishments_narrative: str) -> str:
    narrative = (accomplishments_narrative or "").strip()
    return (
        "Accomplishments\n"
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

    summary_body = render_doc_narrative(compiled["accomplishments_narrative"])
    if not replace_summary_section(document_id, summary_body, service=service):
        return False
    return sync_sheet_sections_into_doc(
        document_id, spreadsheet_id, project_name, service=service
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

        spreadsheet_id = agent_sheets.ensure_spreadsheet(project_name, spreadsheet_id)
        summary_updated = _update_summary_from_sheet(
            document_id, spreadsheet_id, project_name, service
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
