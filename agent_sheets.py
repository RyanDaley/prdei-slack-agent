"""
Google Sheets integration for the project activity log.

The spreadsheet is the source of truth for:
  - Detailed Activity Log rows
  - Total Hours / Actual Hours by Category (formulas)
  - Estimate hours by Category (manual)
  - Bar chart: Actual (color) vs Estimate (gray)

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
CATEGORY_CHART_TITLE = "Actual vs Estimate by Category"
# Dashboard category table: header row 5, data rows 6..(5+N)
CATEGORY_HEADER_ROW = 5

ACTIVITY_HEADERS = [
    "Timestamp",
    "User",
    "Hours",
    "Task",
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


def _category_labels() -> list[str]:
    return list(jm.TASK_CATEGORY_LABELS.values())


def _write_headers_and_dashboard(sheets, spreadsheet_id: str) -> None:
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{ACTIVITY_TAB}!A1:G1",
        valueInputOption="RAW",
        body={"values": [ACTIVITY_HEADERS]},
    ).execute()

    dashboard_top = [
        ["Week Start", ""],
        ["Week Of", ""],
        ["Total Hours", f"=IFERROR(SUMIF({ACTIVITY_TAB}!G:G,B1,{ACTIVITY_TAB}!C:C),0)"],
        [""],
        ["Hours by Category (edit Estimate column manually)", ""],
    ]
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{DASHBOARD_TAB}!A1:B5",
        valueInputOption="USER_ENTERED",
        body={"values": dashboard_top},
    ).execute()
    ensure_category_estimate_table(sheets, spreadsheet_id)


def ensure_category_estimate_table(sheets, spreadsheet_id: str) -> None:
    """
    Category | Actual Hours | Estimate table.
    Actual Hours are formulas; Estimate is manual user input (preserved on update).
    """
    labels = _category_labels()
    last_row = CATEGORY_HEADER_ROW + len(labels)
    existing = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!A{CATEGORY_HEADER_ROW}:C{last_row}",
        )
        .execute()
        .get("values")
        or []
    )

    prior_estimates: dict[str, str | float] = {}
    for row in existing[1:]:
        if not row:
            continue
        label = str(row[0]).strip()
        estimate = row[2] if len(row) > 2 else ""
        if label:
            prior_estimates[label] = estimate

    rows: list[list] = [["Category", "Actual Hours", "Estimate"]]
    for offset, label in enumerate(labels):
        row_num = CATEGORY_HEADER_ROW + 1 + offset
        actual_formula = (
            f"=IFERROR(SUMIFS({ACTIVITY_TAB}!C:C,"
            f"{ACTIVITY_TAB}!E:E,A{row_num},"
            f"{ACTIVITY_TAB}!G:G,$B$1),0)"
        )
        rows.append([label, actual_formula, prior_estimates.get(label, "")])

    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{DASHBOARD_TAB}!A{CATEGORY_HEADER_ROW}:C{last_row}",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


def ensure_category_bar_chart(sheets, spreadsheet_id: str) -> int:
    """
    Ensure a column chart exists: Actual (colored) vs Estimate (gray) by category.
    Returns the Sheets chart ID.
    """
    meta = (
        sheets.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties,charts)",
        )
        .execute()
    )
    dashboard_id = None
    existing_chart_id = None
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") != DASHBOARD_TAB:
            continue
        dashboard_id = int(props["sheetId"])
        for chart in sheet.get("charts") or []:
            title = (chart.get("spec") or {}).get("title", "")
            if title == CATEGORY_CHART_TITLE:
                existing_chart_id = int(chart["chartId"])
                break
    if dashboard_id is None:
        raise ValueError("Dashboard tab missing")

    labels = _category_labels()
    start_row = CATEGORY_HEADER_ROW - 1
    end_row = CATEGORY_HEADER_ROW + len(labels)

    chart_spec = {
        "title": CATEGORY_CHART_TITLE,
        "basicChart": {
            "chartType": "COLUMN",
            "legendPosition": "BOTTOM_LEGEND",
            "headerCount": 1,
            "axis": [
                {"position": "BOTTOM_AXIS", "title": "Category"},
                {"position": "LEFT_AXIS", "title": "Hours"},
            ],
            "domains": [
                {
                    "domain": {
                        "sourceRange": {
                            "sources": [
                                {
                                    "sheetId": dashboard_id,
                                    "startRowIndex": start_row,
                                    "endRowIndex": end_row,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": 1,
                                }
                            ]
                        }
                    }
                }
            ],
            "series": [
                {
                    "series": {
                        "sourceRange": {
                            "sources": [
                                {
                                    "sheetId": dashboard_id,
                                    "startRowIndex": start_row,
                                    "endRowIndex": end_row,
                                    "startColumnIndex": 1,
                                    "endColumnIndex": 2,
                                }
                            ]
                        }
                    },
                    "targetAxis": "LEFT_AXIS",
                    "colorStyle": {
                        "rgbColor": {"red": 0.20, "green": 0.55, "blue": 0.90}
                    },
                },
                {
                    "series": {
                        "sourceRange": {
                            "sources": [
                                {
                                    "sheetId": dashboard_id,
                                    "startRowIndex": start_row,
                                    "endRowIndex": end_row,
                                    "startColumnIndex": 2,
                                    "endColumnIndex": 3,
                                }
                            ]
                        }
                    },
                    "targetAxis": "LEFT_AXIS",
                    "colorStyle": {
                        "rgbColor": {"red": 0.75, "green": 0.75, "blue": 0.75}
                    },
                },
            ],
        },
    }

    if existing_chart_id is not None:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateChartSpec": {
                            "chartId": existing_chart_id,
                            "spec": chart_spec,
                        }
                    }
                ]
            },
        ).execute()
        return existing_chart_id

    response = (
        sheets.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addChart": {
                            "chart": {
                                "spec": chart_spec,
                                "position": {
                                    "overlayPosition": {
                                        "anchorCell": {
                                            "sheetId": dashboard_id,
                                            "rowIndex": CATEGORY_HEADER_ROW
                                            + len(labels)
                                            + 1,
                                            "columnIndex": 0,
                                        },
                                        "widthPixels": 560,
                                        "heightPixels": 360,
                                    }
                                },
                            }
                        }
                    }
                ]
            },
        )
        .execute()
    )
    replies = response.get("replies") or []
    chart_id = replies[0]["addChart"]["chart"]["chartId"]
    print(f"  [SHEETS] Created bar chart '{CATEGORY_CHART_TITLE}' id={chart_id}")
    return int(chart_id)


def _activity_sheet_id(sheets, spreadsheet_id: str) -> int:
    meta = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties)")
        .execute()
    )
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == ACTIVITY_TAB:
            return int(props["sheetId"])
    raise ValueError(f"Tab {ACTIVITY_TAB!r} not found")


def _ensure_activity_headers(sheets, spreadsheet_id: str) -> None:
    """
    Ensure ActivityLog headers include Task (before Category).
    Migrates older 6-column layouts by inserting a Task column at D.
    """
    header_row = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A1:G1")
        .execute()
        .get("values")
        or []
    )
    header = header_row[0] if header_row else []
    if header == ACTIVITY_HEADERS:
        return

    has_task = "Task" in header
    has_category = "Category" in header
    if has_category and not has_task:
        sheet_id = _activity_sheet_id(sheets, spreadsheet_id)
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 3,
                                "endIndex": 4,
                            },
                            "inheritFromBefore": False,
                        }
                    }
                ]
            },
        ).execute()
        print("  [SHEETS] Inserted Task column into ActivityLog")

    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{ACTIVITY_TAB}!A1:G1",
        valueInputOption="RAW",
        body={"values": [ACTIVITY_HEADERS]},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{DASHBOARD_TAB}!B3",
        valueInputOption="USER_ENTERED",
        body={
            "values": [
                [f"=IFERROR(SUMIF({ACTIVITY_TAB}!G:G,B1,{ACTIVITY_TAB}!C:C),0)"]
            ]
        },
    ).execute()


def ensure_spreadsheet(project_name: str, spreadsheet_id: str = "") -> str:
    """
    Ensure ActivityLog + Dashboard tabs/formulas/chart exist.
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
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A1:G1")
        .execute()
        .get("values")
        or []
    )
    if not header:
        _write_headers_and_dashboard(sheets, spreadsheet_id)
    else:
        _ensure_activity_headers(sheets, spreadsheet_id)
        ensure_category_estimate_table(sheets, spreadsheet_id)
    ensure_category_bar_chart(sheets, spreadsheet_id)
    return spreadsheet_id


def get_category_bar_chart_id(spreadsheet_id: str, sheets=None) -> int | None:
    sheets = sheets or get_sheets_service()
    try:
        return ensure_category_bar_chart(sheets, spreadsheet_id)
    except Exception as exc:
        print(f"  [SHEETS WARNING] Could not ensure bar chart: {exc}")
        return None


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
                entry.task_label,
                entry.category_label,
                entry.activity,
                week_start_date,
            ]
        )

    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{ACTIVITY_TAB}!A:G",
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

    header_result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A1:G1")
        .execute()
    )
    header = (header_result.get("values") or [[]])[0]
    has_task_col = "Task" in header

    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A2:G")
        .execute()
    )
    values = result.get("values") or []
    entries: list[jm.LogEntry] = []
    for row in values:
        padded = list(row) + [""] * (7 - len(row))
        if has_task_col:
            timestamp_raw, user, hours_raw, task, category, activity, _week = padded[:7]
        else:
            # Legacy: Timestamp, User, Hours, Category, Activity, WeekStart
            timestamp_raw, user, hours_raw, category, activity, _week = padded[:6]
            task = ""
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
                task=jm.normalize_task_key(str(task)),
                category=jm.normalize_category_key(str(category)),
                activity=str(activity).strip(),
            )
        )
    return jm.filter_entries_for_week(entries, week_start, week_end)


def get_dashboard_category_rows(
    spreadsheet_id: str,
    sheets=None,
) -> list[tuple[str, float, float]]:
    """
    Return [(category_label, actual_hours, estimate_hours), ...] from Dashboard.
    Manual Estimate cells that are blank become 0.0 for charting.
    """
    sheets = sheets or get_sheets_service()
    labels = _category_labels()
    last_row = CATEGORY_HEADER_ROW + len(labels)
    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!A{CATEGORY_HEADER_ROW}:C{last_row}",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )
    values = result.get("values") or []
    rows: list[tuple[str, float, float]] = []
    for row in values[1:]:
        if not row:
            continue
        label = str(row[0]).strip()
        if not label or label.lower() == "category":
            continue
        actual = 0.0
        estimate = 0.0
        if len(row) > 1 and row[1] != "" and row[1] is not None:
            try:
                actual = round(float(row[1]), 2)
            except (TypeError, ValueError):
                actual = 0.0
        if len(row) > 2 and row[2] != "" and row[2] is not None:
            try:
                estimate = round(float(row[2]), 2)
            except (TypeError, ValueError):
                estimate = 0.0
        rows.append((label, actual, estimate))
    return rows


def get_dashboard_totals(
    spreadsheet_id: str,
    sheets=None,
) -> tuple[float, dict[str, float]]:
    sheets = sheets or get_sheets_service()
    labels = _category_labels()
    last_row = CATEGORY_HEADER_ROW + len(labels)
    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!A1:C{last_row}",
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
    # Skip A1:A5 preface; category header is sheet row 5 (index 4).
    for row in values[CATEGORY_HEADER_ROW:]:
        if len(row) < 2:
            continue
        label = str(row[0]).strip()
        if not label or label.lower() == "category":
            continue
        try:
            hours_by_category[label] = round(float(row[1]), 2)
        except (TypeError, ValueError):
            continue
    return total, hours_by_category


def render_category_bar_chart_png(
    category_rows: list[tuple[str, float, float]],
) -> bytes:
    """
    Grouped bar chart: Actual (per-category color) beside Estimate (gray).
    Used for embedding into the Google Doc (Docs API has no linked-chart refresh).
    """
    import io

    from PIL import Image, ImageDraw, ImageFont

    labels = [r[0] for r in category_rows] or ["(none)"]
    actuals = [r[1] for r in category_rows] or [0.0]
    estimates = [r[2] for r in category_rows] or [0.0]

    # Distinct blues/teals for Actual series bars (one per category).
    actual_palette = [
        (51, 140, 230),
        (36, 168, 142),
        (230, 140, 51),
        (120, 90, 200),
        (200, 80, 110),
    ]
    estimate_color = (191, 191, 191)

    width, height = 720, 420
    margin_left, margin_right = 70, 30
    margin_top, margin_bottom = 50, 110
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
        title_font = ImageFont.load_default()
    except Exception:
        font = title_font = None

    draw.text((margin_left, 12), CATEGORY_CHART_TITLE, fill=(40, 40, 40), font=title_font)

    plot_left = margin_left
    plot_right = width - margin_right
    plot_top = margin_top
    plot_bottom = height - margin_bottom
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    max_val = max(max(actuals + estimates + [1.0]), 1.0)
    # Nice upper bound
    nice_max = max_val * 1.15

    n = len(labels)
    group_width = plot_width / max(n, 1)
    bar_width = group_width * 0.32
    gap = group_width * 0.08

    # Axes
    draw.line([(plot_left, plot_top), (plot_left, plot_bottom)], fill=(80, 80, 80), width=1)
    draw.line([(plot_left, plot_bottom), (plot_right, plot_bottom)], fill=(80, 80, 80), width=1)

    for i, (label, actual, estimate) in enumerate(zip(labels, actuals, estimates)):
        group_x = plot_left + i * group_width + group_width * 0.15
        actual_h = (actual / nice_max) * plot_height
        estimate_h = (estimate / nice_max) * plot_height
        actual_color = actual_palette[i % len(actual_palette)]

        ax0 = group_x
        ay0 = plot_bottom - actual_h
        draw.rectangle([ax0, ay0, ax0 + bar_width, plot_bottom], fill=actual_color)

        ex0 = group_x + bar_width + gap
        ey0 = plot_bottom - estimate_h
        draw.rectangle([ex0, ey0, ex0 + bar_width, plot_bottom], fill=estimate_color)

        # Truncate long labels
        short = label if len(label) <= 16 else label[:14] + "…"
        # Approximate text width for centering under the pair of bars
        tx = group_x + bar_width + gap / 2 - (len(short) * 3)
        draw.text((max(plot_left, tx), plot_bottom + 8), short, fill=(50, 50, 50), font=font)

    # Legend
    legend_y = height - 48
    draw.rectangle([margin_left, legend_y, margin_left + 14, legend_y + 14], fill=actual_palette[0])
    draw.text((margin_left + 20, legend_y), "Actual", fill=(50, 50, 50), font=font)
    draw.rectangle(
        [margin_left + 90, legend_y, margin_left + 104, legend_y + 14],
        fill=estimate_color,
    )
    draw.text((margin_left + 110, legend_y), "Estimate", fill=(50, 50, 50), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def trigger_docs_refresh(
    document_id: str,
    spreadsheet_id: str,
    project_name: str,
    webapp_url: str = "",
) -> bool:
    """
    Optional Apps Script webhook. Doc table/chart sync is handled in Python.
    """
    import json
    import os
    import urllib.error
    import urllib.request

    url = (webapp_url or "").strip() or os.environ.get("DOCS_REFRESH_WEBAPP_URL", "").strip()
    if not url:
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
