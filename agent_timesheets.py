"""
Employee weekly timesheet writer (Google Sheets).

Mirrors the legacy Excel layout:

  A2 Employee Name:     B2 Week Ending:
  A3 <name>             B3 <Saturday date>
  Row 4: day date formulas (Sun..Sat)
  Row 5: Project | Task | Category | Activity | Sun..Sat | Total
  Rows 6+: one row per Project + Task + Category
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import google.auth
from googleapiclient.discovery import build

import employee_router
import journal_models as jm
import project_router
import sheets_quota

TIMESHEET_TAB = "Timesheet"
HEADER_ROW = 5
DATA_START_ROW = 6

# Layout: A Project | B Task | C Category | D Activity | E..K Sun..Sat | L Total
DAY_COLUMNS = ["E", "F", "G", "H", "I", "J", "K"]  # Sun..Sat
TOTAL_COLUMN = "L"
LAST_DATA_COLUMN = "L"
NUM_COLUMNS = 12  # A..L

TIMESHEET_HEADERS = [
    "Project",
    "Task",
    "Category",
    "Activity",
    "Sun",
    "Mon",
    "Tue",
    "Wed",
    "Thurs",
    "Fri",
    "Sat",
    "Total",
]

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@dataclass
class TimesheetUpdateResult:
    success: bool
    spreadsheet_id: str = ""
    spreadsheet_title: str = ""
    rows_touched: int = 0
    error_message: str = ""


def get_sheets_service():
    credentials, _ = google.auth.default(scopes=SHEETS_SCOPES)
    return build("sheets", "v4", credentials=credentials)


def _weekday_column(when: datetime) -> str:
    """Map local timestamp to Sun..Sat column letter (E..K)."""
    # Python: Mon=0 .. Sun=6
    mapping = {
        6: "E",  # Sunday
        0: "F",
        1: "G",
        2: "H",
        3: "I",
        4: "J",
        5: "K",
    }
    local = when
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(jm.JOURNAL_TIMEZONE))
    else:
        local = local.astimezone(ZoneInfo(jm.JOURNAL_TIMEZONE))
    return mapping[local.weekday()]


def _day_label(when: datetime) -> str:
    local = when
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(jm.JOURNAL_TIMEZONE))
    else:
        local = local.astimezone(ZoneInfo(jm.JOURNAL_TIMEZONE))
    return local.strftime("%A")


def _ensure_timesheet_tab(sheets, spreadsheet_id: str) -> None:
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets_props = meta.get("sheets", [])
    titles = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheets_props}

    if TIMESHEET_TAB in titles:
        return

    # Rename the first sheet if it's the default Sheet1, else add Timesheet.
    if sheets_props:
        first = sheets_props[0]["properties"]
        first_title = first.get("title", "")
        first_id = first["sheetId"]
        if first_title in ("Sheet1", "Sheet 1") or len(sheets_props) == 1:
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": first_id,
                                    "title": TIMESHEET_TAB,
                                },
                                "fields": "title",
                            }
                        }
                    ]
                },
            ).execute()
            return

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": TIMESHEET_TAB}}}]},
    ).execute()


def _timesheet_sheet_id(sheets, spreadsheet_id: str) -> int:
    meta = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties)")
        .execute()
    )
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == TIMESHEET_TAB:
            return int(props["sheetId"])
    raise ValueError(f"Tab {TIMESHEET_TAB!r} not found")


def _apply_timesheet_formatting(
    sheets,
    spreadsheet_id: str,
    *,
    include_starter_widths: bool = False,
) -> None:
    """
    Apply wrap / bold / alignment. Optionally set starter column widths
    (only when first creating the sheet — later writes use autosize).
    """
    sheet_id = _timesheet_sheet_id(sheets, spreadsheet_id)
    requests = []
    if include_starter_widths:
        widths_px = [
            160,  # Project
            180,  # Task
            140,  # Category
            360,  # Activity
            56,   # Sun
            56,   # Mon
            56,   # Tue
            56,   # Wed
            64,   # Thurs
            56,   # Fri
            56,   # Sat
            64,   # Total
        ]
        for index, pixel_size in enumerate(widths_px):
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": index,
                            "endIndex": index + 1,
                        },
                        "properties": {"pixelSize": pixel_size},
                        "fields": "pixelSize",
                    }
                }
            )
    # Wrap Activity column (D = index 3).
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startColumnIndex": 3,
                    "endColumnIndex": 4,
                    "startRowIndex": HEADER_ROW - 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "TOP",
                    }
                },
                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
            }
        }
    )
    # Bold header labels on row 5 (Project … Total).
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": HEADER_ROW - 1,
                    "endRowIndex": HEADER_ROW,
                    "startColumnIndex": 0,
                    "endColumnIndex": NUM_COLUMNS,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat.textFormat.bold",
            }
        }
    )
    # Bold row 2 labels: Employee Name: / Week Ending:
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": 2,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat.textFormat.bold",
            }
        }
    )
    # Left-align week-ending date (B3) and day dates on row 4 (E4:K4).
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 2,
                    "endRowIndex": 3,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "LEFT",
                    }
                },
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        }
    )
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 3,
                    "endRowIndex": 4,
                    "startColumnIndex": 4,
                    "endColumnIndex": NUM_COLUMNS,
                },
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "LEFT",
                    }
                },
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        }
    )
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


def _autosize_timesheet_columns(
    sheets,
    spreadsheet_id: str,
    *,
    padding_px: int = 24,
) -> None:
    """
    Resize each Timesheet column to fit the longest visible text after a write.

    Uses Sheets autoResize, then also sizes from cell content so wrap/newlines
    and API lag don't leave columns too narrow after a new entry.
    """
    sheet_id = _timesheet_sheet_id(sheets, spreadsheet_id)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": NUM_COLUMNS,  # A..L
                        }
                    }
                }
            ]
        },
    ).execute()

    # Content-aware pass: longest line per column (Activity can have newlines).
    values = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!A2:{LAST_DATA_COLUMN}",
            valueRenderOption="FORMATTED_VALUE",
        )
        .execute()
        .get("values")
        or []
    )
    # Approx px/char for Sheets default font; Activity capped so wrap can work.
    px_per_char = 7.2
    floors = [80, 80, 80, 160, 48, 48, 48, 48, 56, 48, 48, 56]
    ceilings = [280, 280, 220, 480, 90, 90, 90, 90, 96, 90, 90, 96]
    max_lens = [0] * NUM_COLUMNS
    for row in values:
        padded = list(row) + [""] * (NUM_COLUMNS - len(row))
        for col_idx, cell in enumerate(padded[:NUM_COLUMNS]):
            for line in str(cell or "").replace("\r\n", "\n").split("\n"):
                max_lens[col_idx] = max(max_lens[col_idx], len(line.strip()))

    pad_requests = []
    for index, longest in enumerate(max_lens):
        content_px = int(longest * px_per_char) + padding_px
        new_size = max(floors[index], min(ceilings[index], content_px))
        pad_requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"pixelSize": new_size},
                    "fields": "pixelSize",
                }
            }
        )

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": pad_requests},
    ).execute()
    print(
        f"  [TIMESHEET] Auto-sized columns to longest cell content "
        f"(+{padding_px}px padding)",
        flush=True,
    )


def _ensure_category_column(
    sheets, spreadsheet_id: str, header: list[str] | None = None
) -> list[str]:
    """
    Migrate legacy Project|Task|Activity|Sun… layout by inserting Category at C.
    Existing Activity values shift into column D.
    Returns the current header row (after migration if needed).
    """
    if header is None:
        header_row = sheets_quota.execute_with_retry(
            sheets.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{TIMESHEET_TAB}!A5:L5"),
            label="timesheet_header",
        ).get("values") or []
        header = [str(c).strip() for c in (header_row[0] if header_row else [])]
    else:
        header = [str(c).strip() for c in header]

    if not header or "Project" not in header:
        return header
    if "Category" in header:
        return header
    if "Activity" not in header:
        return header

    sheet_id = _timesheet_sheet_id(sheets, spreadsheet_id)
    # Insert before Activity (column index 2).
    sheets_quota.execute_with_retry(
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": 2,
                                "endIndex": 3,
                            },
                            "inheritFromBefore": False,
                        }
                    }
                ]
            },
        ),
        label="timesheet_insert_category",
    )
    sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!A5:L5",
            valueInputOption="RAW",
            body={"values": [TIMESHEET_HEADERS]},
        ),
        label="timesheet_write_headers",
    )
    # Day-date formulas move to E4:K4 after the insert.
    sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!E4:K4",
            valueInputOption="USER_ENTERED",
            body={
                "values": [
                    ["=B3-6", "=B3-5", "=B3-4", "=B3-3", "=B3-2", "=B3-1", "=B3"]
                ]
            },
        ),
        label="timesheet_day_formulas",
    )
    print(f"  [TIMESHEET] Inserted Category column on {spreadsheet_id}", flush=True)
    return list(TIMESHEET_HEADERS)


def ensure_timesheet_layout(
    spreadsheet_id: str,
    employee_name: str,
    week_ending: date,
    sheets=None,
) -> None:
    """Create/refresh header structure if the Timesheet tab is empty."""
    sheets = sheets or get_sheets_service()
    _ensure_timesheet_tab(sheets, spreadsheet_id)

    probe = sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{TIMESHEET_TAB}!A5:L5"),
        label="timesheet_probe_headers",
    ).get("values") or []
    header = [str(c).strip() for c in (probe[0] if probe else [])]
    has_headers = bool(header) and "Project" in header

    # Always refresh name / week ending.
    sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!A2:B3",
            valueInputOption="USER_ENTERED",
            body={
                "values": [
                    ["Employee Name:", "Week Ending:"],
                    [employee_name, week_ending.isoformat()],
                ]
            },
        ),
        label="timesheet_name_week",
    )

    if not has_headers:
        # Row 4 day-date formulas relative to week ending (Sat) in B3.
        # E=Sun (=B3-6) ... K=Sat (=B3)
        day_formulas = [
            ["=B3-6", "=B3-5", "=B3-4", "=B3-3", "=B3-2", "=B3-1", "=B3"],
        ]
        sheets_quota.execute_with_retry(
            sheets.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{TIMESHEET_TAB}!E4:K4",
                valueInputOption="USER_ENTERED",
                body={"values": day_formulas},
            ),
            label="timesheet_init_days",
        )

        sheets_quota.execute_with_retry(
            sheets.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{TIMESHEET_TAB}!A5:L5",
                valueInputOption="RAW",
                body={"values": [TIMESHEET_HEADERS]},
            ),
            label="timesheet_init_headers",
        )
        print(f"  [TIMESHEET] Initialized layout on {spreadsheet_id}")
        _apply_timesheet_formatting(
            sheets, spreadsheet_id, include_starter_widths=True
        )
        return

    header = _ensure_category_column(sheets, spreadsheet_id, header=header)
    # Only rewrite headers when they drift from the canonical layout.
    if header != TIMESHEET_HEADERS:
        sheets_quota.execute_with_retry(
            sheets.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{TIMESHEET_TAB}!A5:L5",
                valueInputOption="RAW",
                body={"values": [TIMESHEET_HEADERS]},
            ),
            label="timesheet_fix_headers",
        )


def _read_data_rows(sheets, spreadsheet_id: str) -> list[list]:
    result = sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!A{DATA_START_ROW}:{LAST_DATA_COLUMN}",
            valueRenderOption="UNFORMATTED_VALUE",
        ),
        label="timesheet_read_rows",
    )
    return result.get("values") or []


def _norm(value: object) -> str:
    return str(value or "").strip()


def _find_row_index(
    rows: list[list],
    project: str,
    task: str,
    category: str = "",
) -> int | None:
    """Return 0-based index into rows for matching Project/Task/Category."""
    target = (
        _norm(project).lower(),
        _norm(task).lower(),
        _norm(category).lower(),
    )
    for idx, row in enumerate(rows):
        padded = list(row) + [""] * (4 - len(row))
        key = (
            _norm(padded[0]).lower(),
            _norm(padded[1]).lower(),
            _norm(padded[2]).lower(),
        )
        if key == target:
            return idx
    return None


def _format_hours_label(hours: float) -> str:
    """Format hours as '1 hr', '0.5 hr', '0.25 hr', etc."""
    return f"{float(hours):g} hr"


_ACTIVITY_LINE_RE = re.compile(
    r"^(?P<day>\w+):\s+(?P<hours>[0-9]*\.?[0-9]+)\s*hr"
    r"(?:\s+[—\-]\s+(?P<text>.*))?$"
)


def _activity_append(
    existing: str,
    when: datetime,
    new_text: str,
    hours: float,
) -> str:
    """
    Build Activity text for a Project/Task row.

    Same weekday as the previous line (same task row): update that line — bump
    hours and append new accomplishment text with a comma (no repeated day).
    Different weekday: start a new line.
    """
    snippet = (new_text or "").strip()
    day = _day_label(when)
    hours_label = _format_hours_label(hours)
    if snippet:
        line = f"{day}: {hours_label} — {snippet}"
    else:
        line = f"{day}: {hours_label}"

    existing = (existing or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    if not existing:
        return line

    lines = existing.split("\n")
    last = lines[-1].strip()
    match = _ACTIVITY_LINE_RE.match(last)
    if not (match and match.group("day") == day):
        return f"{existing}\n{line}"

    try:
        prior_hours = float(match.group("hours"))
    except (TypeError, ValueError):
        prior_hours = 0.0
    total = round(prior_hours + float(hours), 2)
    total_label = _format_hours_label(total)
    prior_text = (match.group("text") or "").strip()

    if not snippet:
        merged_text = prior_text
    elif not prior_text:
        merged_text = snippet
    elif prior_text.lower() == snippet.lower():
        # Identical accomplishment — keep one copy, just roll up hours.
        merged_text = prior_text
    else:
        merged_text = f"{prior_text}, {snippet}"

    if merged_text:
        lines[-1] = f"{day}: {total_label} — {merged_text}"
    else:
        lines[-1] = f"{day}: {total_label}"
    return "\n".join(lines)


def _row_number_for_index(idx: int) -> int:
    return DATA_START_ROW + idx


def _iter_project_row_indices(rows: list[list]) -> list[int]:
    """0-based indices of project data rows (stops before Total / blank gap)."""
    indices: list[int] = []
    for idx, row in enumerate(rows):
        padded = list(row) + [""] * 4
        label = _norm(padded[0])
        if label.lower() == "total":
            break
        if not any(_norm(c) for c in padded[:4]):
            break
        indices.append(idx)
    return indices


def _last_project_sheet_row(sheets, spreadsheet_id: str) -> int | None:
    rows = _read_data_rows(sheets, spreadsheet_id)
    indices = _iter_project_row_indices(rows)
    if not indices:
        return None
    return _row_number_for_index(indices[-1])


def _refresh_totals_row(sheets, spreadsheet_id: str) -> None:
    """
    Place a Total row 3 rows below the last project row.
    Keep normal sheet gridlines; put a bold top border only on the Total row.
    """
    last_project = _last_project_sheet_row(sheets, spreadsheet_id)
    if last_project is None:
        return

    total_row = last_project + 3
    sheet_id = _timesheet_sheet_id(sheets, spreadsheet_id)
    existing = _read_data_rows(sheets, spreadsheet_id)

    # Rows that may still carry a leftover Total top-border from an earlier position.
    stale_border_rows: set[int] = set()
    for idx, row in enumerate(existing):
        label = _norm(row[0] if row else "")
        row_num = _row_number_for_index(idx)
        if label.lower() == "total" and row_num != total_row:
            stale_border_rows.add(row_num)
            sheets.spreadsheets().values().clear(
                spreadsheetId=spreadsheet_id,
                range=f"{TIMESHEET_TAB}!A{row_num}:{LAST_DATA_COLUMN}{row_num}",
            ).execute()

    # Blank padding rows between last project and Total (and slightly past, in case
    # Total shifted down from a previous refresh).
    for blank_row in range(last_project + 1, total_row):
        stale_border_rows.add(blank_row)
        sheets.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!A{blank_row}:{LAST_DATA_COLUMN}{blank_row}",
        ).execute()

    start = DATA_START_ROW
    end = last_project
    # A Total | B | C Category | D Activity | E..K days | L row-total
    values = [[
        "Total",
        "",
        "",
        "",
        f"=IFERROR(SUM(E{start}:E{end}),0)",
        f"=IFERROR(SUM(F{start}:F{end}),0)",
        f"=IFERROR(SUM(G{start}:G{end}),0)",
        f"=IFERROR(SUM(H{start}:H{end}),0)",
        f"=IFERROR(SUM(I{start}:I{end}),0)",
        f"=IFERROR(SUM(J{start}:J{end}),0)",
        f"=IFERROR(SUM(K{start}:K{end}),0)",
        f"=IFERROR(SUM(L{start}:L{end}),0)",
    ]]
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{TIMESHEET_TAB}!A{total_row}:{LAST_DATA_COLUMN}{total_row}",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    requests: list[dict] = [
        # Keep normal sheet gridlines visible in the timesheet tab.
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"hideGridlines": False},
                },
                "fields": "gridProperties.hideGridlines",
            }
        },
        # Drop any previous user-entered border overrides (including STYLE NONE
        # wipe from older code) so the default grid shows again.
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": HEADER_ROW - 1,
                    "endRowIndex": total_row + 5,
                    "startColumnIndex": 0,
                    "endColumnIndex": NUM_COLUMNS,
                },
                "cell": {},
                "fields": "userEnteredFormat.borders",
            }
        },
    ]

    # Bold top border across A:L on the Total row only.
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": total_row - 1,
                    "endRowIndex": total_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": NUM_COLUMNS,
                },
                "cell": {
                    "userEnteredFormat": {
                        "borders": {
                            "top": {
                                "style": "SOLID",
                                "width": 2,
                                "color": {"red": 0, "green": 0, "blue": 0},
                            }
                        },
                        "textFormat": {"bold": True},
                    }
                },
                "fields": (
                    "userEnteredFormat.borders.top,"
                    "userEnteredFormat.textFormat.bold"
                ),
            }
        }
    )

    # Clear bold/borders on stale Total positions.
    for stale_row in stale_border_rows:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": stale_row - 1,
                        "endRowIndex": stale_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": NUM_COLUMNS,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": False},
                        }
                    },
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }
        )

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


def upsert_timesheet_entry(
    spreadsheet_id: str,
    project_name: str,
    task_label: str,
    activity: str,
    hours: float,
    when: datetime,
    category_label: str = "",
    sheets=None,
    *,
    rows: list[list] | None = None,
) -> list[list]:
    """
    Add hours to the Project+Task+Category row for the entry's weekday,
    and append the accomplishment into the Activity column (always column D).
    Category may be blank; Activity is never written into the Category column.

    Returns the in-memory data rows (refreshed after write) so callers can
    reuse them across multiple entries without re-reading the sheet.
    """
    sheets = sheets or get_sheets_service()
    category_label = str(category_label or "").strip()
    activity = str(activity or "").strip()
    if rows is None:
        rows = _read_data_rows(sheets, spreadsheet_id)
    project_indices = _iter_project_row_indices(rows)
    # Only search project rows for matches (ignore Total / gap).
    search_rows = [rows[i] for i in project_indices]
    idx_in_search = _find_row_index(
        search_rows, project_name, task_label, category_label
    )
    day_col = _weekday_column(when)

    if idx_in_search is None:
        last_project = (
            _row_number_for_index(project_indices[-1]) if project_indices else None
        )
        new_row_num = (last_project + 1) if last_project else DATA_START_ROW

        day_values = {col: "" for col in DAY_COLUMNS}
        day_values[day_col] = hours
        activity_text = _activity_append("", when, activity, hours)
        values = [[
            project_name,
            task_label,
            category_label,  # C — blank when no category
            activity_text,   # D — always the accomplishment
            day_values["E"] or "",
            day_values["F"] or "",
            day_values["G"] or "",
            day_values["H"] or "",
            day_values["I"] or "",
            day_values["J"] or "",
            day_values["K"] or "",
            f"=IFERROR(SUM(E{new_row_num}:K{new_row_num}),0)",
        ]]
        sheets_quota.execute_with_retry(
            sheets.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{TIMESHEET_TAB}!A{new_row_num}:{LAST_DATA_COLUMN}{new_row_num}",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            ),
            label="timesheet_insert_row",
        )
        # Activity newlines: rewrite Activity (D) with RAW
        sheets_quota.execute_with_retry(
            sheets.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{TIMESHEET_TAB}!D{new_row_num}",
                valueInputOption="RAW",
                body={"values": [[activity_text]]},
            ),
            label="timesheet_write_activity",
        )
        # Keep local cache in sync for subsequent entries in this request.
        insert_at = len(project_indices)
        rows = list(rows)
        rows.insert(insert_at, list(values[0]))
        return rows

    idx = project_indices[idx_in_search]
    row_num = _row_number_for_index(idx)
    row = list(rows[idx]) + [""] * (NUM_COLUMNS - len(rows[idx]))
    day_index = {
        "E": 4,
        "F": 5,
        "G": 6,
        "H": 7,
        "I": 8,
        "J": 9,
        "K": 10,
    }[day_col]
    try:
        current = float(row[day_index] or 0)
    except (TypeError, ValueError):
        current = 0.0
    new_hours = round(current + float(hours), 2)
    new_activity = _activity_append(str(row[3] or ""), when, activity, hours)

    sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!D{row_num}",
            valueInputOption="RAW",
            body={"values": [[new_activity]]},
        ),
        label="timesheet_update_activity",
    )
    sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!{day_col}{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [[new_hours]]},
        ),
        label="timesheet_update_hours",
    )
    sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{TIMESHEET_TAB}!{TOTAL_COLUMN}{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [[f"=IFERROR(SUM(E{row_num}:K{row_num}),0)"]]},
        ),
        label="timesheet_update_total",
    )
    row[3] = new_activity
    row[day_index] = new_hours
    rows = list(rows)
    rows[idx] = row
    return rows


def process_timesheet_update(
    slack_user_id: str,
    employee_display_name: str,
    entries: list[jm.LogEntry],
    last_name: str = "",
) -> TimesheetUpdateResult:
    """
    Write journal entries into this week's employee timesheet Sheet.
    """
    if not entries:
        return TimesheetUpdateResult(success=True, error_message="No entries.")

    try:
        title_name = employee_display_name or last_name or "Employee"
        assets = employee_router.ensure_employee_timesheet(slack_user_id, title_name)
        if not assets:
            return TimesheetUpdateResult(
                success=False,
                error_message=(
                    "No employee timesheet Drive folder configured "
                    f"(Firestore User or EMPLOYEE_FOLDER_{slack_user_id.upper()})."
                ),
            )

        sheets = get_sheets_service()
        ensure_timesheet_layout(
            assets.spreadsheet_id,
            employee_display_name,
            assets.week_ending,
            sheets=sheets,
        )

        touched = 0
        cached_rows: list[list] | None = None
        for entry in entries:
            project_name = project_router.get_project_display_name(entry.project_key)
            cached_rows = upsert_timesheet_entry(
                assets.spreadsheet_id,
                project_name=project_name,
                task_label=entry.task_label or entry.task,
                category_label=entry.category_label or "",
                activity=entry.activity,
                hours=entry.hours,
                when=entry.timestamp,
                sheets=sheets,
                rows=cached_rows,
            )
            touched += 1

        if touched:
            _refresh_totals_row(sheets, assets.spreadsheet_id)
            # Skip autosize on the hot Slack path — it costs extra read quota
            # (60 reads/min/user) and isn't required for correct logging.

        print(
            f"  [TIMESHEET SUCCESS] Wrote {touched} entr(y/ies) to "
            f"'{assets.spreadsheet_title}' ({assets.spreadsheet_id})"
        )
        return TimesheetUpdateResult(
            success=True,
            spreadsheet_id=assets.spreadsheet_id,
            spreadsheet_title=assets.spreadsheet_title,
            rows_touched=touched,
        )
    except Exception as exc:
        print(f"  [TIMESHEET ERROR] {exc}")
        message = str(exc)
        if "storageQuotaExceeded" in message:
            message = (
                "Drive storageQuotaExceeded — the timesheet folder must live on a "
                "Shared Drive, and the Cloud Run service account needs Content manager "
                "access there (My Drive / 'shared with me' folders use the SA's zero quota)."
            )
        return TimesheetUpdateResult(success=False, error_message=message)
