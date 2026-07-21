"""
Google Sheets integration for the project activity log.

Firestore is the source of truth for logged activity (time_logs) and task
Completed $ / Avg Weekly Spend. Sheets are a working view:
  - ActivityLog rows (written from Slack for the weekly narrative/chart)
  - Dashboard: week rollups + PM-edited Estimated $ / project dates
  - Category/Task Budget Completed $ are SUMIF(S) from ActivityLog
    (task total = sum of its categories + uncategorized logs)
  - Avg Weekly Spend is filled from Firestore Completed $

Per-project sheets live in the project Drive folder and are named
"Detailed Activity Log" (see project_router.ensure_project_assets).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import google.auth
from googleapiclient.discovery import build

import firestore_store
import journal_models as jm
import project_router
import sheets_quota

SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ACTIVITY_TAB = "ActivityLog"
DASHBOARD_TAB = "Dashboard"
TASK_CHART_TITLE = "Task Budget Progress ($)"
# Legacy titles — delete/replace when rebuilding the chart
_LEGACY_CHART_TITLES = {
    TASK_CHART_TITLE,
    "Completed vs Estimated by Category ($)",
    "Actual vs Estimate by Category",
}

ACTIVITY_HEADERS = [
    "Timestamp",
    "User",
    "Hours",
    "Task",
    "Category",
    "Activity",
    "WeekStart",
    "Rate",
    "Amount",
]

# Dashboard layout anchors (employee table starts here; category/task follow dynamically)
EMPLOYEE_SECTION_TITLE_ROW = 6
EMPLOYEE_HEADER_ROW = 7
# Hidden-ish metadata cell for project scoping across refreshes
PROJECT_KEY_CELL = f"{DASHBOARD_TAB}!H1"

# Match Google Doc Heading 1: 14 pt, bold, PRDEI blue #45B0E1
DASHBOARD_H1_RGB = (69, 176, 225)
DASHBOARD_H1_FONT_PT = 14
DASHBOARD_SECTION_PREFIXES = (
    "Hours by Employee / Task / Category",
    "Category Budget",
    "Task Budget",
)


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


def _sheet_str_literal(value: str) -> str:
    """Escape a string for use inside a Sheets formula."""
    return '"' + str(value).replace('"', '""') + '"'


def _category_labels() -> list[str]:
    """Legacy helper — prefer _category_labels_for_project when project is known."""
    return list(jm.category_labels_map().values())


def _category_budget_items(
    project_key: str = "",
) -> list[tuple[str, str, str]]:
    """
    Category Budget rows for a project.
    Returns [(display_label, task_name, category_name), ...].
    Display uses "Task / Category" when the same category name exists on
    more than one task in the project.
    """
    project_key = (project_key or "").strip()
    if not project_key:
        return [(name, "", name) for name in _category_labels()]
    try:
        cats = firestore_store.list_categories_for_project(project_key)
        counts: dict[str, int] = {}
        for c in cats:
            counts[c.name] = counts.get(c.name, 0) + 1
        task_names = {t.id: t.name for t in firestore_store.list_tasks(project_key)}
        items: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for c in cats:
            task_name = task_names.get(c.task_id) or ""
            if counts[c.name] > 1 and task_name:
                label = f"{task_name} / {c.name}"
            else:
                label = c.name
            if label in seen:
                continue
            seen.add(label)
            items.append((label, task_name, c.name))
        return items
    except Exception as exc:
        print(f"  [SHEETS WARNING] Could not list project categories: {exc}", flush=True)
        return []


def _category_labels_for_project(project_key: str = "") -> list[str]:
    """Display names of categories belonging to this project's tasks."""
    return [label for label, _task, _cat in _category_budget_items(project_key)]


def _task_labels_for_project(
    project_key: str = "",
    *,
    activity_task_names: set[str] | None = None,
) -> list[str]:
    """
    Tasks for this project's sheet only.
    Never falls back to the global all-projects task map.
    """
    labels: list[str] = []
    if project_key:
        try:
            tasks = firestore_store.list_tasks(project_key)
            labels = [t.name for t in tasks]
        except Exception as exc:
            print(f"  [SHEETS WARNING] Could not list project tasks: {exc}", flush=True)
    # Include names logged on this sheet that aren't in Firestore yet (same project only).
    for name in sorted(activity_task_names or []):
        if name and name not in labels and name != "(no tasks yet)":
            labels.append(name)
    return labels


def _read_stored_project_key(sheets, spreadsheet_id: str) -> str:
    try:
        result = (
            sheets.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=PROJECT_KEY_CELL)
            .execute()
        )
        values = result.get("values") or []
        if values and values[0]:
            return str(values[0][0]).strip()
    except Exception:
        pass
    return ""


def _resolve_project_key(
    sheets, spreadsheet_id: str, project_key: str = ""
) -> str:
    key = (project_key or "").strip() or _read_stored_project_key(sheets, spreadsheet_id)
    if key:
        try:
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=PROJECT_KEY_CELL,
                valueInputOption="RAW",
                body={"values": [[key]]},
            ).execute()
        except Exception:
            pass
    return key


def _write_headers_and_dashboard(
    sheets, spreadsheet_id: str, project_key: str = ""
) -> None:
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{ACTIVITY_TAB}!A1:I1",
        valueInputOption="RAW",
        body={"values": [ACTIVITY_HEADERS]},
    ).execute()
    refresh_dashboard_tables(sheets, spreadsheet_id, project_key=project_key)


def _read_week_start(sheets, spreadsheet_id: str) -> str:
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{DASHBOARD_TAB}!B1")
        .execute()
    )
    values = result.get("values") or []
    if values and values[0]:
        return str(values[0][0]).strip()
    week_start, _, _ = jm.get_current_week_range()
    return week_start.date().isoformat()


def _read_activity_rows(sheets, spreadsheet_id: str) -> list[list]:
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A2:I")
        .execute()
    )
    return result.get("values") or []


def _prior_estimates_by_label(
    sheets, spreadsheet_id: str, start_row: int, label_col: str = "A", estimate_col_index: int = 3
) -> dict[str, str | float]:
    """
    Read an existing budget table and return {label: estimated}.
    estimate_col_index is 0-based within the fetched A:D range.
    """
    try:
        result = (
            sheets.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=f"{DASHBOARD_TAB}!A{start_row}:D{start_row + 80}",
            )
            .execute()
        )
    except Exception:
        return {}
    prior: dict[str, str | float] = {}
    for row in result.get("values") or []:
        if not row:
            continue
        label = str(row[0]).strip()
        if not label or label.lower() in {
            "category",
            "task",
            "employee",
            "hours by employee / task / category",
            "category budget (edit estimated $ manually)",
            "task budget (edit estimated $ manually)",
        }:
            continue
        estimate = row[estimate_col_index] if len(row) > estimate_col_index else ""
        if label:
            prior[label] = estimate
    return prior


def _read_project_schedule_from_sheet(
    sheets, spreadsheet_id: str
) -> tuple[str, str]:
    """
    Read Project Start Date / Estimated End Date from Dashboard header cells.
    Returns YYYY-MM-DD strings (Sheets serials / locale dates are normalized).
    """
    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!A1:B20",
            valueRenderOption="FORMATTED_VALUE",
        )
        .execute()
    )
    start_date = ""
    end_date = ""
    for row in result.get("values") or []:
        if not row:
            continue
        label = str(row[0]).strip().lower()
        value = row[1] if len(row) > 1 else ""
        if label in {"project start date", "start date"}:
            start_date = firestore_store.normalize_project_date(value)
        elif label in {
            "project estimated end date",
            "estimated end date",
            "project end date",
        }:
            end_date = firestore_store.normalize_project_date(value)
    return start_date, end_date


def _read_task_budget_from_sheet(
    sheets, spreadsheet_id: str
) -> dict[str, dict[str, int | None]]:
    """
    Read the Task Budget table from Dashboard before it is rebuilt.
    Returns {task_name: {estimated, completed, avg_weekly_spend}}.
    """
    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!A1:H200",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )
    values = result.get("values") or []
    _, section = _find_dashboard_section(values, "Task")
    out: dict[str, dict[str, int | None]] = {}
    for row in section[1:]:
        if not row:
            continue
        label = str(row[0]).strip()
        if not label or label.lower() in {"task", "(no tasks yet)"}:
            continue
        estimated = None
        completed = None
        avg_weekly = None
        if len(row) > 3 and row[3] not in ("", None):
            estimated = _as_money(row[3])
        if len(row) > 2 and row[2] not in ("", None):
            completed = _as_money(row[2])
        # Avg Weekly Spend is column F (index 5) when Remaining is column E.
        if len(row) > 5 and row[5] not in ("", None):
            avg_weekly = _as_money(row[5])
        out[label] = {
            "estimated": estimated,
            "completed": completed,
            "avg_weekly_spend": avg_weekly,
        }
    return out


def _activity_completed_by_task(activity_rows: list[list]) -> dict[str, int]:
    """Lifetime Completed $ per task name from ActivityLog Amount column."""
    totals: dict[str, float] = {}
    for row in activity_rows:
        padded = list(row) + [""] * (9 - len(row))
        task = str(padded[3] or "").strip()
        if not task:
            continue
        try:
            amount = float(padded[8] or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            try:
                hours = float(padded[2] or 0)
                rate = float(padded[7] or 0)
                amount = hours * rate
            except (TypeError, ValueError):
                amount = 0.0
        totals[task] = totals.get(task, 0.0) + amount
    return {name: int(round(val)) for name, val in totals.items()}


def refresh_dashboard_tables(
    sheets,
    spreadsheet_id: str,
    *,
    project_key: str = "",
    autosize: bool = True,
) -> None:
    """
    Rebuild Dashboard tables from ActivityLog + Firestore.

    autosize=False skips column autosize (saves read quota on Slack write paths).
    """
    sheets_quota.call_with_retry(
        lambda: _refresh_dashboard_tables_impl(
            sheets,
            spreadsheet_id,
            project_key=project_key,
            autosize=autosize,
        ),
        label="refresh_dashboard",
    )


def _refresh_dashboard_tables_impl(
    sheets,
    spreadsheet_id: str,
    *,
    project_key: str = "",
    autosize: bool = True,
) -> None:
    """
    Rebuild Dashboard:
      - Week header + Total Hours / Total Completed ($)
      - Project Start Date / Estimated End Date (Sheet edit → Firestore)
      - Employee × Task × Category hours (and Completed $) for the week
      - Category budget: Hours | Completed ($) | Estimated ($)  (week-scoped)
      - Task budget: Hours | Completed ($) from Firestore | Estimated ($)
        (PM edit) | Remaining | Avg Weekly Spend (from Firestore completed)

    ActivityLog is not pushed into Firestore. Slack → time_logs is the
    activity source of truth; this refresh reads task money fields from FS.
    """
    project_key = _resolve_project_key(sheets, spreadsheet_id, project_key)
    week_start = _read_week_start(sheets, spreadsheet_id)
    week_label = ""
    try:
        wl = (
            sheets.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{DASHBOARD_TAB}!B2")
            .execute()
            .get("values")
            or []
        )
        if wl and wl[0]:
            week_label = str(wl[0][0])
    except Exception:
        week_label = ""

    # --- Capture PM-typed values BEFORE clearing the Dashboard ---
    sheet_start, sheet_end = _read_project_schedule_from_sheet(sheets, spreadsheet_id)
    sheet_task_budget = _read_task_budget_from_sheet(sheets, spreadsheet_id)
    prior_cat = _prior_estimates_by_label(sheets, spreadsheet_id, 5)
    prior_task: dict[str, str | float] = {}
    for label, data in sheet_task_budget.items():
        if data.get("estimated") is not None:
            prior_task[label] = data["estimated"]
    for label, est in prior_cat.items():
        prior_task.setdefault(label, est)

    activity = _read_activity_rows(sheets, spreadsheet_id)

    # Firestore is primary for schedule + task completed / estimated / avg weekly
    fs_project_start = ""
    fs_project_end = ""
    fs_cat_est: dict[str, int] = {}
    fs_task_est: dict[str, int] = {}
    fs_task_completed: dict[str, int] = {}
    fs_task_avg: dict[str, int] = {}
    try:
        if project_key:
            proj = firestore_store.get_project(project_key)
            if proj:
                fs_project_start = firestore_store.normalize_project_date(
                    proj.start_date or ""
                )
                fs_project_end = firestore_store.normalize_project_date(
                    proj.estimated_end_date or ""
                )
            for task in firestore_store.list_tasks(project_key):
                fs_task_est[task.name] = task.estimated
                fs_task_completed[task.name] = int(task.completed or 0)
                fs_task_avg[task.name] = int(task.avg_weekly_spend or 0)
        for cat in firestore_store.list_categories_for_project(project_key) if project_key else []:
            fs_cat_est[cat.name] = cat.estimated
        if not project_key:
            for cat in firestore_store.list_categories():
                fs_cat_est[cat.name] = cat.estimated
    except Exception as exc:
        print(f"  [SHEETS WARNING] Firestore lookup failed: {exc}", flush=True)

    # Prefer Firestore ISO dates; accept Sheet edits only when they normalize
    # to a real date (never let a Sheets serial overwrite a good ISO value).
    project_start = fs_project_start or sheet_start or ""
    project_end = fs_project_end or sheet_end or ""
    if sheet_start and sheet_start != fs_project_start:
        project_start = sheet_start
    if sheet_end and sheet_end != fs_project_end:
        project_end = sheet_end
    project_start = firestore_store.normalize_project_date(project_start)
    project_end = firestore_store.normalize_project_date(project_end)

    # Sync PM edits only (schedule + Estimated $) — not ActivityLog completed.
    if project_key:
        try:
            firestore_store.ensure_project_schedule_fields()
            firestore_store.ensure_task_money_fields()
            firestore_store.set_project_schedule(
                project_key,
                start_date=project_start,
                estimated_end_date=project_end,
            )
            estimated_by_name = {
                name: int(data["estimated"])
                for name, data in sheet_task_budget.items()
                if data.get("estimated") is not None
            }
            written = firestore_store.sync_project_task_budget(
                project_key,
                estimated_by_name=estimated_by_name,
            )
            if written:
                print(
                    f"  [SHEETS] Synced {written} Estimated $ field(s) to Firestore "
                    f"for project `{project_key}`",
                    flush=True,
                )
                # Refresh local estimate cache after sync
                for task in firestore_store.list_tasks(project_key):
                    fs_task_est[task.name] = task.estimated
        except Exception as exc:
            print(f"  [SHEETS WARNING] Sheet→Firestore estimate sync failed: {exc}", flush=True)

    # Aggregate employee × task × category for this week (display only)
    emp_agg: dict[tuple[str, str, str], list[float]] = {}
    seen_tasks: set[str] = set()
    for row in activity:
        padded = list(row) + [""] * (9 - len(row))
        user, hours_raw, task, category, _act, row_week = (
            padded[1],
            padded[2],
            padded[3],
            padded[4],
            padded[5],
            padded[6],
        )
        task_name = str(task).strip()
        if task_name:
            seen_tasks.add(task_name)
        if str(row_week).strip() != week_start:
            continue
        try:
            hours = float(hours_raw or 0)
        except (TypeError, ValueError):
            hours = 0.0
        try:
            amount = float(padded[8] or 0) if len(padded) > 8 else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            try:
                rate = float(padded[7] or 0)
            except (TypeError, ValueError):
                rate = 0.0
            amount = round(hours * rate, 2)
        key = (str(user).strip(), task_name, str(category).strip())
        if not key[0]:
            continue
        bucket = emp_agg.setdefault(key, [0.0, 0.0])
        bucket[0] = round(bucket[0] + hours, 2)
        bucket[1] = round(bucket[1] + amount, 2)

    emp_rows_sorted = sorted(
        emp_agg.items(),
        key=lambda kv: (kv[0][0].lower(), kv[0][1].lower(), kv[0][2].lower()),
    )

    category_items = _category_budget_items(project_key)
    category_labels = [label for label, _task, _cat in category_items]
    task_labels = _task_labels_for_project(
        project_key, activity_task_names=seen_tasks
    )
    if not project_key:
        print(
            "  [SHEETS WARNING] No project_key for dashboard; "
            "Task Budget limited to names found in this sheet's ActivityLog.",
            flush=True,
        )

    # Avg Weekly Spend from Firestore Completed $ (not ActivityLog)
    avg_weekly_by_task: dict[str, int] = {}
    for label in task_labels:
        completed = int(fs_task_completed.get(label, 0) or 0)
        avg_weekly_by_task[label] = compute_avg_weekly_spend(
            completed,
            project_start=project_start,
            activity_rows=activity,
            task_name=label,
            as_of=week_start,
        )

    # Build dashboard values as one contiguous block from row 1
    values: list[list] = [
        ["Week Start", week_start],
        ["Week Of", week_label],
        [
            "Total Hours",
            f"=IFERROR(SUMIF({ACTIVITY_TAB}!G:G,B1,{ACTIVITY_TAB}!C:C),0)",
        ],
        [
            "Total Completed ($)",
            f"=IFERROR(SUMIF({ACTIVITY_TAB}!G:G,B1,{ACTIVITY_TAB}!I:I),0)",
        ],
        ["Project Start Date", project_start],
        ["Project Estimated End Date", project_end],
        [""],
        ["Hours by Employee / Task / Category", "", "", "", ""],
        ["Employee", "Task", "Category", "Hours", "Completed ($)"],
    ]

    if emp_rows_sorted:
        for (user, task, category), (hours, amount) in emp_rows_sorted:
            values.append([user, task, category, hours, amount])
    else:
        values.append(["(no entries this week)", "", "", 0, 0])

    values.append([""])
    values.append(
        [
            "Category Budget (Completed $ = Activity Log for that task+category)",
            "",
            "",
            "",
        ]
    )
    cat_header_row = len(values) + 1  # 1-based sheet row of next append
    values.append(["Category", "Hours", "Completed ($)", "Estimated ($)"])
    for offset, (label, task_name, cat_name) in enumerate(category_items):
        row_num = cat_header_row + 1 + offset
        # Lifetime totals scoped to the owning task so duplicate category names
        # across tasks do not mix. Sum of a task's categories (+ blank category
        # rows) equals that task's Task Budget Completed $.
        cat_lit = _sheet_str_literal(cat_name)
        if task_name:
            task_lit = _sheet_str_literal(task_name)
            hours_f = (
                f"=IFERROR(SUMIFS({ACTIVITY_TAB}!C:C,"
                f"{ACTIVITY_TAB}!D:D,{task_lit},"
                f"{ACTIVITY_TAB}!E:E,{cat_lit}),0)"
            )
            completed_f = (
                f"=IFERROR(SUMIFS({ACTIVITY_TAB}!I:I,"
                f"{ACTIVITY_TAB}!D:D,{task_lit},"
                f"{ACTIVITY_TAB}!E:E,{cat_lit}),0)"
            )
        else:
            hours_f = (
                f"=IFERROR(SUMIF({ACTIVITY_TAB}!E:E,{cat_lit},{ACTIVITY_TAB}!C:C),0)"
            )
            completed_f = (
                f"=IFERROR(SUMIF({ACTIVITY_TAB}!E:E,{cat_lit},{ACTIVITY_TAB}!I:I),0)"
            )
        estimate = prior_cat.get(label, "") or prior_cat.get(cat_name, "")
        if estimate in ("", None) and cat_name in fs_cat_est and fs_cat_est[cat_name]:
            estimate = fs_cat_est[cat_name]
        elif estimate in ("", None) and label in fs_cat_est and fs_cat_est[label]:
            estimate = fs_cat_est[label]
        values.append([label, hours_f, completed_f, estimate])

    values.append([""])
    values.append(
        ["Task Budget (edit Estimated $ manually)", "", "", "", "", ""]
    )
    task_header_row = len(values) + 1
    values.append(
        [
            "Task",
            "Hours",
            "Completed ($)",
            "Estimated ($)",
            "Remaining ($)",
            "Avg Weekly Spend ($)",
        ]
    )
    if not task_labels:
        values.append(["(no tasks yet)", 0, 0, "", 0, ""])
    else:
        for offset, label in enumerate(task_labels):
            row_num = task_header_row + 1 + offset
            hours_f = (
                f"=IFERROR(SUMIF({ACTIVITY_TAB}!D:D,A{row_num},{ACTIVITY_TAB}!C:C),0)"
            )
            # Lifetime Completed $ from Activity Log (= sum of that task's
            # categorized + uncategorized rows).
            completed_f = (
                f"=IFERROR(SUMIF({ACTIVITY_TAB}!D:D,A{row_num},{ACTIVITY_TAB}!I:I),0)"
            )
            remaining_f = f"=MAX(0,D{row_num}-C{row_num})"
            estimate = prior_task.get(label, "")
            if estimate in ("", None) and label in fs_task_est and fs_task_est[label]:
                estimate = fs_task_est[label]
            avg_spend = avg_weekly_by_task.get(label, 0) or ""
            values.append(
                [label, hours_f, completed_f, estimate, remaining_f, avg_spend]
            )

    # Clear old dashboard content then write
    sheets.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"{DASHBOARD_TAB}!A1:H200",
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{DASHBOARD_TAB}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    # Keep schedule dates as plain YYYY-MM-DD text (USER_ENTERED would turn
    # them into Sheets serials like 46219 and corrupt Firestore on the next sync).
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{DASHBOARD_TAB}!B5:B6",
        valueInputOption="RAW",
        body={"values": [[project_start], [project_end]]},
    ).execute()
    if project_key:
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=PROJECT_KEY_CELL,
            valueInputOption="RAW",
            body={"values": [[project_key]]},
        ).execute()

    # Sync category estimates + task Estimated $ from rebuilt values
    _sync_estimates_to_firestore(
        category_labels=category_labels,
        task_labels=task_labels,
        prior_cat={**fs_cat_est, **{k: _as_money(v) for k, v in prior_cat.items()}},
        values=values,
        project_key=project_key,
    )
    # Persist auto avg weekly spend back to Firestore (derived from FS completed)
    if project_key and avg_weekly_by_task:
        try:
            firestore_store.sync_project_task_budget(
                project_key,
                avg_weekly_spend_by_name=dict(avg_weekly_by_task),
            )
        except Exception as exc:
            print(
                f"  [SHEETS WARNING] Task avg weekly re-sync failed: {exc}",
                flush=True,
            )

    task_header_idx = next(
        (
            i
            for i, r in enumerate(values)
            if r and r[0] == "Task" and len(r) > 1 and r[1] == "Hours"
        ),
        None,
    )
    if task_header_idx is not None:
        ensure_task_progress_chart(
            sheets,
            spreadsheet_id,
            header_row=task_header_idx + 1,
            num_tasks=max(len(task_labels), 1),
        )

    _apply_dashboard_formatting(sheets, spreadsheet_id, values)
    if autosize:
        _autosize_dashboard_columns(sheets, spreadsheet_id)
        try:
            _autosize_activity_log_columns(sheets, spreadsheet_id)
        except Exception as exc:
            print(f"  [SHEETS WARNING] ActivityLog autosize failed: {exc}", flush=True)


def _dashboard_sheet_id(sheets, spreadsheet_id: str) -> int:
    meta = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties)")
        .execute()
    )
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == DASHBOARD_TAB:
            return int(props["sheetId"])
    raise ValueError(f"Tab {DASHBOARD_TAB!r} not found")


def _is_dashboard_section_title(cell: str) -> bool:
    text = (cell or "").strip()
    return any(text.startswith(prefix) for prefix in DASHBOARD_SECTION_PREFIXES)


def _is_dashboard_column_header_row(row: list) -> bool:
    cells = [str(c).strip() for c in row]
    if not cells:
        return False
    if cells[:5] == ["Employee", "Task", "Category", "Hours", "Completed ($)"]:
        return True
    if cells[:4] == ["Category", "Hours", "Completed ($)", "Estimated ($)"]:
        return True
    if (
        len(cells) >= 6
        and cells[0] == "Task"
        and cells[1] == "Hours"
        and cells[2] == "Completed ($)"
    ):
        return True
    return False


def _apply_dashboard_formatting(
    sheets,
    spreadsheet_id: str,
    values: list[list],
) -> None:
    """
    Style Dashboard section titles like Doc Heading 1, and bold table headers.
    """
    sheet_id = _dashboard_sheet_id(sheets, spreadsheet_id)
    r, g, b = DASHBOARD_H1_RGB
    h1_format = {
        "textFormat": {
            "bold": True,
            "fontSize": DASHBOARD_H1_FONT_PT,
            "foregroundColor": {
                "red": r / 255.0,
                "green": g / 255.0,
                "blue": b / 255.0,
            },
        }
    }
    requests: list[dict] = [
        # Reset formatting so row shifts from rebuilds don't leave stale styles.
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": max(len(values) + 5, 40),
                    "startColumnIndex": 0,
                    "endColumnIndex": 6,
                },
                "cell": {"userEnteredFormat": {}},
                "fields": "userEnteredFormat",
            }
        }
    ]

    for row_idx, row in enumerate(values):
        if not row:
            continue
        first = str(row[0]).strip()
        if _is_dashboard_section_title(first):
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx,
                            "endRowIndex": row_idx + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        },
                        "cell": {"userEnteredFormat": h1_format},
                        "fields": (
                            "userEnteredFormat.textFormat.bold,"
                            "userEnteredFormat.textFormat.fontSize,"
                            "userEnteredFormat.textFormat.foregroundColor"
                        ),
                    }
                }
            )
            continue
        if _is_dashboard_column_header_row(row):
            end_col = max(len(row), 5)
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx,
                            "endRowIndex": row_idx + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": end_col,
                        },
                        "cell": {
                            "userEnteredFormat": {"textFormat": {"bold": True}}
                        },
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                }
            )

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()
    print("  [SHEETS] Applied Dashboard section/header formatting", flush=True)


def _autosize_columns_with_padding(
    sheets,
    spreadsheet_id: str,
    *,
    sheet_id: int,
    tab_title: str,
    num_cols: int,
    value_range: str,
    padding_px: int = 24,
    floors: list[int] | None = None,
    ceilings: list[int] | None = None,
) -> None:
    """
    Sheets autoResize, then size from longest cell content + padding
    (same approach as the timesheet spreadsheet).
    """
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
                            "endIndex": num_cols,
                        }
                    }
                }
            ]
        },
    ).execute()

    values = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=value_range,
            valueRenderOption="FORMATTED_VALUE",
        )
        .execute()
        .get("values")
        or []
    )
    px_per_char = 7.2
    floors = floors or ([80] * num_cols)
    ceilings = ceilings or ([320] * num_cols)
    floors = (floors + [80] * num_cols)[:num_cols]
    ceilings = (ceilings + [320] * num_cols)[:num_cols]
    max_lens = [0] * num_cols
    for row in values:
        padded = list(row) + [""] * (num_cols - len(row))
        for col_idx, cell in enumerate(padded[:num_cols]):
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
        f"  [SHEETS] Auto-sized {tab_title} columns "
        f"(+{padding_px}px padding)",
        flush=True,
    )


def _autosize_dashboard_columns(sheets, spreadsheet_id: str) -> None:
    _autosize_columns_with_padding(
        sheets,
        spreadsheet_id,
        sheet_id=_dashboard_sheet_id(sheets, spreadsheet_id),
        tab_title=DASHBOARD_TAB,
        num_cols=6,  # A..F (Task Budget widest table)
        value_range=f"{DASHBOARD_TAB}!A1:F200",
        padding_px=24,
        floors=[120, 72, 110, 110, 110, 150],
        ceilings=[360, 120, 160, 160, 160, 200],
    )


def _autosize_activity_log_columns(sheets, spreadsheet_id: str) -> None:
    _autosize_columns_with_padding(
        sheets,
        spreadsheet_id,
        sheet_id=_activity_sheet_id(sheets, spreadsheet_id),
        tab_title=ACTIVITY_TAB,
        num_cols=9,  # A..I
        value_range=f"{ACTIVITY_TAB}!A1:I",
        padding_px=24,
        floors=[120, 80, 56, 100, 100, 160, 90, 56, 72],
        ceilings=[220, 200, 80, 240, 240, 420, 130, 80, 110],
    )


def _as_money(value) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _weeks_elapsed_inclusive(start_iso: str, as_of_iso: str) -> int:
    """
    Calendar weeks from start through as_of (inclusive), minimum 1.
    Day 0–6 of the project = week 1, day 7–13 = week 2, etc.
    """
    from datetime import date as date_cls

    start_iso = (start_iso or "").strip()
    as_of_iso = (as_of_iso or "").strip()
    if not start_iso or not as_of_iso:
        return 1
    try:
        start_d = date_cls.fromisoformat(start_iso[:10])
        as_of_d = date_cls.fromisoformat(as_of_iso[:10])
    except ValueError:
        return 1
    if as_of_d < start_d:
        return 1
    return max(1, (as_of_d - start_d).days // 7 + 1)


def _earliest_activity_week_for_task(
    activity_rows: list[list], task_name: str
) -> str:
    """Earliest ActivityLog Week Start for a task (YYYY-MM-DD), or \"\"."""
    task_name = (task_name or "").strip()
    earliest = ""
    for row in activity_rows:
        padded = list(row) + [""] * (9 - len(row))
        if str(padded[3] or "").strip() != task_name:
            continue
        week = str(padded[6] or "").strip()
        if week and (not earliest or week < earliest):
            earliest = week
    return earliest


def compute_avg_weekly_spend(
    completed: int,
    *,
    project_start: str = "",
    activity_rows: list[list] | None = None,
    task_name: str = "",
    as_of: str = "",
) -> int:
    """
    Average weekly spend ($) = completed / weeks elapsed since project start.

    Only calculated when completed > 0; otherwise 0. If project start is blank,
    falls back to the earliest ActivityLog week for that task.
    """
    completed = int(completed or 0)
    if completed <= 0:
        return 0
    start = (project_start or "").strip()
    if not start and activity_rows is not None and task_name:
        start = _earliest_activity_week_for_task(activity_rows, task_name)
    as_of = (as_of or "").strip()
    if not as_of:
        as_of = datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE)).date().isoformat()
    if not start:
        start = as_of
    weeks = _weeks_elapsed_inclusive(start, as_of)
    return int(round(completed / weeks))


def _sync_estimates_to_firestore(
    *,
    category_labels: list[str],
    task_labels: list[str],
    prior_cat: dict,
    values: list[list],
    project_key: str,
) -> None:
    """Write Estimated $ from the just-built dashboard rows into Firestore."""
    # Build label → estimate from values rows that look like budget data
    estimates: dict[str, int] = {}
    mode = None
    for row in values:
        if not row:
            continue
        head = str(row[0]).strip()
        if head == "Category" and len(row) > 1 and row[1] == "Hours":
            mode = "category"
            continue
        if head == "Task" and len(row) > 1 and row[1] == "Hours":
            mode = "task"
            continue
        if head.startswith("Category Budget") or head.startswith("Task Budget") or head.startswith("Hours by"):
            mode = None
            continue
        if mode and head and len(row) > 3:
            raw = row[3]
            if raw in ("", None):
                continue
            estimates[f"{mode}:{head}"] = _as_money(raw)

    try:
        cats = (
            firestore_store.list_categories_for_project(project_key)
            if project_key
            else firestore_store.list_categories()
        )
        task_names = (
            {t.id: t.name for t in firestore_store.list_tasks(project_key)}
            if project_key
            else {}
        )
        cat_by_name: dict[str, object] = {}
        cat_by_task_and_name: dict[tuple[str, str], object] = {}
        for c in cats:
            cat_by_name[c.name] = c
            tname = task_names.get(getattr(c, "task_id", ""), "")
            if tname:
                cat_by_task_and_name[(tname, c.name)] = c
        for label in category_labels:
            est = estimates.get(f"category:{label}")
            if est is None:
                continue
            cat = None
            if " / " in label:
                task_part, cat_part = label.split(" / ", 1)
                cat = cat_by_task_and_name.get((task_part, cat_part))
            if cat is None:
                lookup = label.split(" / ", 1)[-1] if " / " in label else label
                cat = cat_by_name.get(lookup) or cat_by_name.get(label)
            if cat and cat.estimated != est:
                firestore_store.set_category_estimated(cat.id, est)
        if project_key:
            task_by_name = {t.name: t for t in firestore_store.list_tasks(project_key)}
            for label in task_labels:
                est = estimates.get(f"task:{label}")
                if est is None:
                    continue
                task = task_by_name.get(label)
                if task and task.estimated != est:
                    firestore_store.set_task_estimated(task.id, est)
    except Exception as exc:
        print(f"  [SHEETS WARNING] Could not sync estimates to Firestore: {exc}", flush=True)


def ensure_category_estimate_table(
    sheets, spreadsheet_id: str, project_key: str = ""
) -> None:
    """Back-compat alias: rebuild full dashboard budget tables."""
    refresh_dashboard_tables(sheets, spreadsheet_id, project_key=project_key)


def ensure_task_progress_chart(
    sheets,
    spreadsheet_id: str,
    header_row: int | None = None,
    num_tasks: int | None = None,
) -> int:
    """
    Stacked column chart per task: Completed $ (blue) + Remaining $ (gray).
    Total bar height ≈ Estimated; blue fills up as work completes.
    header_row is 1-based Dashboard row of the Task header.
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
    delete_ids: list[int] = []
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") != DASHBOARD_TAB:
            continue
        dashboard_id = int(props["sheetId"])
        for chart in sheet.get("charts") or []:
            title = (chart.get("spec") or {}).get("title", "")
            chart_id = int(chart["chartId"])
            if title == TASK_CHART_TITLE:
                existing_chart_id = chart_id
            elif title in _LEGACY_CHART_TITLES:
                delete_ids.append(chart_id)
    if dashboard_id is None:
        raise ValueError("Dashboard tab missing")

    if delete_ids:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [{"deleteEmbeddedObject": {"objectId": cid}} for cid in delete_ids]
            },
        ).execute()

    if header_row is None:
        # Best-effort: find Task header on the sheet
        header_row = EMPLOYEE_HEADER_ROW
        try:
            result = (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=f"{DASHBOARD_TAB}!A1:A200")
                .execute()
            )
            for i, row in enumerate(result.get("values") or [], start=1):
                if row and str(row[0]).strip() == "Task":
                    # Confirm next cells via a wider read would be ideal; accept first Task
                    header_row = i
                    # Prefer the Task Budget header (after Category). Keep scanning.
        except Exception:
            pass
    if num_tasks is None:
        num_tasks = 1

    start_row = header_row - 1  # 0-based
    end_row = header_row + num_tasks

    chart_spec = {
        "title": TASK_CHART_TITLE,
        "basicChart": {
            "chartType": "COLUMN",
            "legendPosition": "BOTTOM_LEGEND",
            "headerCount": 1,
            "stackedType": "STACKED",
            "axis": [
                {"position": "BOTTOM_AXIS", "title": "Task"},
                {"position": "LEFT_AXIS", "title": "Dollars"},
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
                    # Completed ($) — column C (index 2) — blue fill from bottom
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
                        "rgbColor": {"red": 0.20, "green": 0.55, "blue": 0.90}
                    },
                },
                {
                    # Remaining ($) — column E (index 4) — gray remainder of Estimated
                    "series": {
                        "sourceRange": {
                            "sources": [
                                {
                                    "sheetId": dashboard_id,
                                    "startRowIndex": start_row,
                                    "endRowIndex": end_row,
                                    "startColumnIndex": 4,
                                    "endColumnIndex": 5,
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
                                            "rowIndex": end_row + 1,
                                            "columnIndex": 0,
                                        },
                                        "widthPixels": 640,
                                        "heightPixels": 380,
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
    print(f"  [SHEETS] Created bar chart '{TASK_CHART_TITLE}' id={chart_id}")
    return int(chart_id)


def ensure_category_bar_chart(
    sheets,
    spreadsheet_id: str,
    header_row: int | None = None,
    num_categories: int | None = None,
) -> int:
    """Back-compat: dashboard chart is now the task progress chart."""
    return ensure_task_progress_chart(
        sheets,
        spreadsheet_id,
        header_row=header_row,
        num_tasks=num_categories,
    )


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
    Ensure ActivityLog headers include Task, Rate, and Amount.
    Migrates older layouts by inserting missing columns.
    """
    header_row = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A1:I1")
        .execute()
        .get("values")
        or []
    )
    header = header_row[0] if header_row else []
    if header == ACTIVITY_HEADERS:
        return

    sheet_id = _activity_sheet_id(sheets, spreadsheet_id)
    has_task = "Task" in header
    has_category = "Category" in header
    if has_category and not has_task:
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
        header = (
            sheets.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A1:I1")
            .execute()
            .get("values")
            or [[]]
        )[0]

    # Ensure Rate + Amount columns exist at H and I.
    while len(header) < 9:
        insert_at = len(header)
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": insert_at,
                                "endIndex": insert_at + 1,
                            },
                            "inheritFromBefore": False,
                        }
                    }
                ]
            },
        ).execute()
        header = list(header) + [""]

    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{ACTIVITY_TAB}!A1:I1",
        valueInputOption="RAW",
        body={"values": [ACTIVITY_HEADERS]},
    ).execute()
    print("  [SHEETS] Updated ActivityLog headers to include Rate/Amount")


def _format_log_timestamp(when: datetime) -> str:
    tz = ZoneInfo(jm.JOURNAL_TIMEZONE)
    local = when
    if isinstance(local, datetime):
        if local.tzinfo is None:
            local = local.replace(tzinfo=tz)
        else:
            local = local.astimezone(tz)
        return local.strftime("%Y-%m-%d %I:%M %p")
    return str(when or "")


def seed_activity_log_from_firestore(
    spreadsheet_id: str,
    project_key: str,
    sheets=None,
    *,
    limit: int = 5000,
) -> int:
    """
    Populate an empty ActivityLog from Firestore time_logs for this project.
    Returns number of rows written. Safe to call only when the sheet has no
    activity rows yet (e.g. newly recreated Detailed Activity Log).
    """
    project_key = (project_key or "").strip()
    if not project_key or not spreadsheet_id:
        return 0
    sheets = sheets or get_sheets_service()

    try:
        logs = firestore_store.list_time_logs(project_key=project_key, limit=limit)
    except Exception as exc:
        print(
            f"  [SHEETS WARNING] Could not list Firestore time_logs for "
            f"`{project_key}`: {exc}",
            flush=True,
        )
        return 0
    if not logs:
        print(
            f"  [SHEETS] No Firestore time_logs to seed for `{project_key}`",
            flush=True,
        )
        return 0

    # list_time_logs is newest-first; ActivityLog should read oldest→newest.
    logs = list(reversed(logs))

    user_cache: dict[str, tuple[str, int]] = {}

    def _user_display_and_rate(user_id: str) -> tuple[str, int]:
        uid = (user_id or "").strip()
        if uid in user_cache:
            return user_cache[uid]
        display = uid or "Unknown"
        rate = firestore_store.DEFAULT_USER_RATE
        try:
            rec = firestore_store.get_user(uid) if uid else None
            if rec:
                display = rec.display_name or rec.email or uid or display
                if rec.rate and rec.rate > 0:
                    rate = int(rec.rate)
        except Exception:
            pass
        user_cache[uid] = (display, rate)
        return display, rate

    rows: list[list] = []
    for log in logs:
        when = log.logged_at
        if not isinstance(when, datetime):
            when = datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE))
        week_start, _, _ = jm.get_current_week_range(reference=when)
        display, rate = _user_display_and_rate(log.user_id)
        hours = float(log.hours or 0)
        amount = int(round(hours * rate))
        rows.append(
            [
                _format_log_timestamp(when),
                display,
                hours,
                log.task or "",
                log.category or "",
                log.activity or "",
                week_start.date().isoformat(),
                rate,
                amount,
            ]
        )

    sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{ACTIVITY_TAB}!A2",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ),
        label="seed_activity_log",
    )
    refresh_dashboard_tables(
        sheets, spreadsheet_id, project_key=project_key, autosize=False
    )
    print(
        f"  [SHEETS] Seeded ActivityLog with {len(rows)} Firestore time_log(s) "
        f"for `{project_key}`",
        flush=True,
    )
    return len(rows)


def ensure_spreadsheet(
    project_name: str,
    spreadsheet_id: str = "",
    project_key: str = "",
) -> tuple[str, bool]:
    """
    Ensure ActivityLog + Dashboard tabs/formulas/chart exist.

    Returns (spreadsheet_id, seeded_from_firestore).
    When a new/empty ActivityLog is initialized, historical time_logs for the
    project are copied from Firestore so deleting the Sheet does not lose history.
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
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A1:I1")
        .execute()
        .get("values")
        or []
    )
    seeded = False
    if not header:
        _write_headers_and_dashboard(sheets, spreadsheet_id, project_key=project_key)
        if project_key:
            seeded = seed_activity_log_from_firestore(
                spreadsheet_id, project_key, sheets=sheets
            ) > 0
    else:
        # Headers only — do not rebuild the Dashboard here. Callers that write
        # ActivityLog should refresh once after the write (quota-sensitive).
        _ensure_activity_headers(sheets, spreadsheet_id)
        if project_key:
            activity_rows = _read_activity_rows(sheets, spreadsheet_id)
            if not activity_rows:
                seeded = seed_activity_log_from_firestore(
                    spreadsheet_id, project_key, sheets=sheets
                ) > 0
    return spreadsheet_id, seeded


def get_category_bar_chart_id(spreadsheet_id: str, sheets=None) -> int | None:
    sheets = sheets or get_sheets_service()
    try:
        return ensure_task_progress_chart(sheets, spreadsheet_id)
    except Exception as exc:
        print(f"  [SHEETS WARNING] Could not ensure bar chart: {exc}")
        return None


def update_dashboard_week(
    spreadsheet_id: str,
    week_start: datetime,
    week_label: str,
    sheets=None,
    project_key: str = "",
    *,
    refresh: bool = False,
) -> None:
    """
    Write Week Start / Week Of on the Dashboard.

    refresh=False by default so Slack log paths can update the week cells
    cheaply and call refresh_dashboard_tables once after ActivityLog writes.
    """
    sheets = sheets or get_sheets_service()
    week_start_date = week_start.date().isoformat()
    sheets_quota.execute_with_retry(
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!B1:B2",
            valueInputOption="USER_ENTERED",
            body={"values": [[week_start_date], [week_label]]},
        ),
        label="update_dashboard_week",
    )
    if refresh:
        refresh_dashboard_tables(
            sheets, spreadsheet_id, project_key=project_key, autosize=False
        )


def append_log_entries(
    spreadsheet_id: str,
    entries: list[jm.LogEntry],
    sheets=None,
    rate: int = 0,
    project_key: str = "",
) -> bool:
    """
    Write ActivityLog rows (including Rate and Amount = hours × rate).
    If a row this week already matches (user + task + category + activity text),
    add hours (and Amount) to that row instead of inserting a duplicate.
    """
    if not entries:
        return True
    sheets = sheets or get_sheets_service()
    week_start, _, _ = jm.get_current_week_range()
    week_start_date = week_start.date().isoformat()
    rate = int(rate or 0)

    existing = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"{ACTIVITY_TAB}!A2:I")
        .execute()
        .get("values")
        or []
    )

    def _norm_text(value: object) -> str:
        return " ".join(str(value or "").strip().split()).lower()

    def _find_match_row(
        user: str,
        task_label: str,
        category_label: str,
        activity: str,
    ) -> int | None:
        """1-based sheet row number of the latest matching row this week."""
        target = (
            _norm_text(user),
            _norm_text(task_label),
            _norm_text(category_label),
            _norm_text(activity),
        )
        for idx in range(len(existing) - 1, -1, -1):
            padded = list(existing[idx]) + [""] * (9 - len(existing[idx]))
            row_week = str(padded[6] or "").strip()
            if row_week and row_week != week_start_date:
                continue
            key = (
                _norm_text(padded[1]),
                _norm_text(padded[3]),
                _norm_text(padded[4]),
                _norm_text(padded[5]),
            )
            if key == target:
                return idx + 2  # header is row 1
        return None

    to_append: list[list] = []
    for entry in entries:
        task_label = entry.task_label or ""
        # Always reserve the Category cell (blank when optional/unset) so Activity
        # never shifts left into the Category column.
        category_label = str(entry.category_label or "")
        activity_text = str(entry.activity or "")
        amount = int(round(float(entry.hours) * rate))
        match_row = _find_match_row(
            entry.user, task_label, category_label, activity_text
        )
        if match_row is None:
            row = [
                entry.timestamp_str,
                entry.user,
                entry.hours,
                task_label,
                category_label,
                activity_text,
                week_start_date,
                rate,
                amount,
            ]
            to_append.append(row)
            existing.append(row)
            continue

        padded = list(existing[match_row - 2]) + [""] * 9
        try:
            current_hours = float(padded[2] or 0)
        except (TypeError, ValueError):
            current_hours = 0.0
        new_hours = round(current_hours + float(entry.hours), 2)
        try:
            row_rate = int(float(padded[7] or rate or 0))
        except (TypeError, ValueError):
            row_rate = rate
        if row_rate <= 0:
            row_rate = rate
        new_amount = int(round(new_hours * row_rate))
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{ACTIVITY_TAB}!A{match_row}:I{match_row}",
            valueInputOption="USER_ENTERED",
            body={
                "values": [
                    [
                        entry.timestamp_str,
                        entry.user,
                        new_hours,
                        task_label,
                        category_label,
                        activity_text,
                        week_start_date,
                        row_rate,
                        new_amount,
                    ]
                ],
            },
        ).execute()
        existing[match_row - 2] = [
            entry.timestamp_str,
            entry.user,
            new_hours,
            task_label,
            category_label,
            activity_text,
            week_start_date,
            row_rate,
            new_amount,
        ]
        print(
            f"  [SHEETS] Merged {entry.hours:g} hr into ActivityLog row {match_row} "
            f"(now {new_hours:g} hr / ${new_amount})",
            flush=True,
        )

    if to_append:
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{ACTIVITY_TAB}!A:I",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": to_append},
        ).execute()

    refresh_dashboard_tables(
        sheets, spreadsheet_id, project_key=project_key, autosize=False
    )
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


def _find_dashboard_section(
    values: list[list], header_label: str
) -> tuple[int, list[list]]:
    """
    Find a budget table whose header row starts with header_label ('Category' or 'Task')
    and next cell is 'Hours'. Returns (0-based header index, data rows including header).
    """
    for i, row in enumerate(values):
        if (
            row
            and str(row[0]).strip() == header_label
            and len(row) > 1
            and str(row[1]).strip() == "Hours"
        ):
            data = [row]
            for nxt in values[i + 1 :]:
                if not nxt or not str(nxt[0]).strip():
                    break
                head = str(nxt[0]).strip()
                if head.startswith("Task Budget") or head.startswith("Category Budget"):
                    break
                if head in ("Task", "Category", "Employee") and len(nxt) > 1 and str(nxt[1]).strip() == "Hours":
                    break
                data.append(nxt)
            return i, data
    return -1, []


def get_dashboard_category_rows(
    spreadsheet_id: str,
    sheets=None,
) -> list[tuple[str, float, float]]:
    """
    Return [(category_label, completed_dollars, estimated_dollars), ...] from Category Budget.
    """
    sheets = sheets or get_sheets_service()
    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!A1:E200",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )
    values = result.get("values") or []
    _, section = _find_dashboard_section(values, "Category")
    rows: list[tuple[str, float, float]] = []
    for row in section[1:]:
        if not row:
            continue
        label = str(row[0]).strip()
        if not label or label.lower() == "category":
            continue
        completed = 0.0
        estimate = 0.0
        money_idx = 2 if len(row) > 2 else 1
        est_idx = 3 if len(row) > 3 else 2
        if len(row) > money_idx and row[money_idx] not in ("", None):
            try:
                completed = round(float(row[money_idx]), 2)
            except (TypeError, ValueError):
                completed = 0.0
        if len(row) > est_idx and row[est_idx] not in ("", None):
            try:
                estimate = round(float(row[est_idx]), 2)
            except (TypeError, ValueError):
                estimate = 0.0
        rows.append((label, completed, estimate))
    return rows


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
            range=f"{DASHBOARD_TAB}!A1:E200",
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
    _, section = _find_dashboard_section(values, "Category")
    for row in section[1:]:
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


def get_dashboard_task_rows(
    spreadsheet_id: str,
    sheets=None,
) -> list[tuple[str, float, float]]:
    """
    Return [(task_label, completed_dollars, estimated_dollars), ...] from Task Budget.
    Used for the progress chart image in the Google Doc.
    """
    sheets = sheets or get_sheets_service()
    result = (
        sheets.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{DASHBOARD_TAB}!A1:E200",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )
    values = result.get("values") or []
    _, section = _find_dashboard_section(values, "Task")
    rows: list[tuple[str, float, float]] = []
    for row in section[1:]:
        if not row:
            continue
        label = str(row[0]).strip()
        if not label or label.lower() in {"task", "(no tasks yet)"}:
            continue
        completed = 0.0
        estimate = 0.0
        if len(row) > 2 and row[2] not in ("", None):
            try:
                completed = round(float(row[2]), 2)
            except (TypeError, ValueError):
                completed = 0.0
        if len(row) > 3 and row[3] not in ("", None):
            try:
                estimate = round(float(row[3]), 2)
            except (TypeError, ValueError):
                estimate = 0.0
        rows.append((label, completed, estimate))
    return rows


def render_category_bar_chart_png(
    category_rows: list[tuple[str, float, float]],
) -> bytes:
    """Back-compat alias — renders the task progress chart style."""
    return render_task_progress_chart_png(category_rows)


def render_task_progress_chart_png(
    task_rows: list[tuple[str, float, float]],
) -> bytes:
    """
    Progress bars per task: gray Estimated full height, blue Completed filling from bottom.
    task_rows: [(label, completed_$, estimated_$), ...]
    """
    import io

    from PIL import Image, ImageDraw, ImageFont

    labels = [r[0] for r in task_rows] or ["(none)"]
    completed_vals = [max(0.0, float(r[1])) for r in task_rows] or [0.0]
    estimate_vals = [max(0.0, float(r[2])) for r in task_rows] or [0.0]

    completed_color = (51, 140, 230)
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

    draw.text((margin_left, 12), TASK_CHART_TITLE, fill=(40, 40, 40), font=title_font)

    plot_left = margin_left
    plot_right = width - margin_right
    plot_top = margin_top
    plot_bottom = height - margin_bottom
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    max_val = max(max(estimate_vals + completed_vals + [1.0]), 1.0)
    nice_max = max_val * 1.15

    n = len(labels)
    group_width = plot_width / max(n, 1)
    bar_width = group_width * 0.45

    draw.line([(plot_left, plot_top), (plot_left, plot_bottom)], fill=(80, 80, 80), width=1)
    draw.line([(plot_left, plot_bottom), (plot_right, plot_bottom)], fill=(80, 80, 80), width=1)

    for i, (label, completed, estimate) in enumerate(
        zip(labels, completed_vals, estimate_vals)
    ):
        # Full estimated bar (gray), then completed overlay (blue) from the bottom.
        bar_height_est = (estimate / nice_max) * plot_height if estimate else 0
        bar_height_done = (min(completed, estimate or completed) / nice_max) * plot_height
        # If completed exceeds estimate, grow the blue bar above the gray.
        if completed > estimate:
            bar_height_done = (completed / nice_max) * plot_height
            bar_height_est = max(bar_height_est, bar_height_done)

        group_x = plot_left + i * group_width + (group_width - bar_width) / 2
        # Gray estimated (background)
        ey0 = plot_bottom - bar_height_est
        draw.rectangle(
            [group_x, ey0, group_x + bar_width, plot_bottom],
            fill=estimate_color,
        )
        # Blue completed (fills from bottom)
        cy0 = plot_bottom - bar_height_done
        if bar_height_done > 0:
            draw.rectangle(
                [group_x, cy0, group_x + bar_width, plot_bottom],
                fill=completed_color,
            )

        short = label if len(label) <= 16 else label[:14] + "…"
        tx = group_x + bar_width / 2 - (len(short) * 3)
        draw.text((max(plot_left, tx), plot_bottom + 8), short, fill=(50, 50, 50), font=font)

    legend_y = height - 48
    draw.rectangle(
        [margin_left, legend_y, margin_left + 14, legend_y + 14], fill=completed_color
    )
    draw.text((margin_left + 20, legend_y), "Completed $", fill=(50, 50, 50), font=font)
    draw.rectangle(
        [margin_left + 120, legend_y, margin_left + 134, legend_y + 14],
        fill=estimate_color,
    )
    draw.text((margin_left + 140, legend_y), "Estimated $", fill=(50, 50, 50), font=font)

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
