import json
import os
import re
import threading
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import agent_journal
import agent_timesheets
import drive_picker
import employee_router
import firestore_store
import hourly_reminder
import journal_models as jm
import project_router

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

FALLBACK_PROJECT_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Tahoe Backyard"}, "value": "tahoe_backyard"},
    {"text": {"type": "plain_text", "text": "Wood Energy Facility"}, "value": "wood_energy_facility"},
    {"text": {"type": "plain_text", "text": "8494 Speckled Ave"}, "value": "8494_speckled"},
]

BREAK_PROJECT_VALUE = "break"
BREAK_PROJECT_OPTION = {
    "text": {"type": "plain_text", "text": "Break"},
    "value": BREAK_PROJECT_VALUE,
}

# Slack static_select requires ≥1 option; these are placeholders, not real tasks.
TASK_OPTION_NEED_PROJECT = {
    "text": {"type": "plain_text", "text": "Select a project first"},
    "value": "_need_project",
}
TASK_OPTION_EMPTY = {
    "text": {"type": "plain_text", "text": "No tasks yet — use +"},
    "value": "_no_tasks",
}
PLACEHOLDER_TASK_VALUES = {TASK_OPTION_NEED_PROJECT["value"], TASK_OPTION_EMPTY["value"]}

CATEGORY_OPTION_NEED_TASK = {
    "text": {"type": "plain_text", "text": "Select a task first"},
    "value": "_need_task",
}
CATEGORY_OPTION_EMPTY = {
    "text": {"type": "plain_text", "text": "No categories yet — use +"},
    "value": "_no_categories",
}
CATEGORY_OPTION_NONE = {
    "text": {"type": "plain_text", "text": "(no category)"},
    "value": "_none",
}
PLACEHOLDER_CATEGORY_VALUES = {
    CATEGORY_OPTION_NEED_TASK["value"],
    CATEGORY_OPTION_EMPTY["value"],
    CATEGORY_OPTION_NONE["value"],
}

ENTRY_DURATION_OPTIONS = [
    {"text": {"type": "plain_text", "text": "0.25 hr"}, "value": "0.25"},
    {"text": {"type": "plain_text", "text": "0.5 hr"}, "value": "0.5"},
    {"text": {"type": "plain_text", "text": "1.0 hr"}, "value": "1.0"},
    {"text": {"type": "plain_text", "text": "1.25 hr"}, "value": "1.25"},
    {"text": {"type": "plain_text", "text": "1.5 hr"}, "value": "1.5"},
    {"text": {"type": "plain_text", "text": "1.75 hr"}, "value": "1.75"},
    {"text": {"type": "plain_text", "text": "2.0 hr"}, "value": "2.0"},
]
ENTRY_DURATION_VALUES = {opt["value"] for opt in ENTRY_DURATION_OPTIONS}
DEFAULT_ENTRY_DURATION = "1.0"
MIN_ENTRY_ROWS = 1
MAX_ENTRY_ROWS = 12


def _option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": value}


def _project_options(*, allow_break: bool) -> list[dict]:
    options = list(FALLBACK_PROJECT_OPTIONS)
    try:
        projects = firestore_store.list_projects()
        if projects:
            options = [_option(p.name, p.id) for p in projects]
    except Exception as exc:
        print(f"[SLACK] Firestore project options failed; using fallback: {exc}", flush=True)
    if allow_break:
        return [*options, BREAK_PROJECT_OPTION]
    return options


def _task_options(project_id: str | None = None) -> list[dict]:
    """Tasks for one project. Placeholders when project is missing/Break."""
    if not project_id or project_id == BREAK_PROJECT_VALUE:
        return [TASK_OPTION_NEED_PROJECT]
    try:
        tasks = firestore_store.list_tasks(project_id)
        if tasks:
            return [_option(t.name, t.id) for t in tasks]
        return [TASK_OPTION_EMPTY]
    except Exception as exc:
        print(f"[SLACK] Firestore task options failed; using empty placeholder: {exc}", flush=True)
        return [TASK_OPTION_EMPTY]


def _category_options(task_id: str | None = None) -> list[dict]:
    """Categories for one task. Optional — always allow '(no category)' once a task is set."""
    if not task_id or task_id in PLACEHOLDER_TASK_VALUES:
        return [CATEGORY_OPTION_NEED_TASK]
    try:
        categories = firestore_store.list_categories(task_id)
        if categories:
            return [CATEGORY_OPTION_NONE, *[_option(c.name, c.id) for c in categories]]
        return [CATEGORY_OPTION_NONE, CATEGORY_OPTION_EMPTY]
    except Exception as exc:
        print(
            f"[SLACK] Firestore category options failed; using empty placeholder: {exc}",
            flush=True,
        )
        return [CATEGORY_OPTION_NONE, CATEGORY_OPTION_EMPTY]


def _create_plus_button(action_id: str, value: str, accessibility_label: str) -> dict:
    # Slack has no true hover tooltip; accessibility_label is the descriptive label.
    return {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": "+"},
        "value": value,
        "accessibility_label": accessibility_label[:75],
    }


def _open_logtime_modal(client, trigger_id: str, channel_id: str, user_id: str):
    preserve_state = None
    row_count = MIN_ENTRY_ROWS  # Always open with a single entry slot.
    try:
        last = firestore_store.get_last_logtime(user_id)
        if last and last.entries:
            preserve_state = _preserve_state_from_last_logtime(last)
            print(
                f"[SLACK] Prefilling logtime for {user_id} from last Entry 1.",
                flush=True,
            )
    except Exception as exc:
        print(f"[SLACK WARNING] Could not load last logtime for {user_id}: {exc}", flush=True)

    modal = build_logtime_modal(row_count=row_count, preserve_state=preserve_state)
    modal["private_metadata"] = json.dumps(
        _logtime_private_metadata(
            channel_id=channel_id,
            user_id=user_id,
            row_count=row_count,
            preserve_state=preserve_state,
        )
    )
    client.views_open(trigger_id=trigger_id, view=modal)


def _preserve_state_from_last_logtime(last: firestore_store.LastLogtime) -> dict:
    """
    Prefill Entry 1 only from the previous submission.
    Extra entries from prior submits are intentionally ignored.
    """
    state: dict = {}
    if not last.entries:
        return state
    entry = last.entries[0]
    row_index = 0
    project_key = entry.project_key or ""
    task_id = entry.task or ""
    # Legacy global task keys → project-scoped document ids for prefill.
    if (
        project_key
        and project_key != BREAK_PROJECT_VALUE
        and task_id
        and "__" not in task_id
    ):
        task_id = firestore_store.task_doc_id(project_key, task_id)

    (
        project_block_id,
        task_block_id,
        category_block_id,
        accomplishment_block_id,
    ) = _entry_block_ids(row_index, project_key=project_key, task_key=task_id)
    duration_block_id = _duration_block_id(row_index)
    hours = entry.hours if entry.hours in ENTRY_DURATION_VALUES else DEFAULT_ENTRY_DURATION
    state[duration_block_id] = {
        "entry_duration_select": {"selected_option": {"value": hours}}
    }
    if project_key:
        state[project_block_id] = {
            "project_select": {"selected_option": {"value": project_key}}
        }
    if task_id:
        state[task_block_id] = {
            "task_select": {"selected_option": {"value": task_id}}
        }
    if entry.category:
        category_id = entry.category
        # Legacy global category keys → task-scoped document ids for prefill.
        if (
            task_id
            and "__" not in category_id
            and task_id not in PLACEHOLDER_TASK_VALUES
        ):
            category_id = firestore_store.category_doc_id(task_id, category_id)
        category_block_id = _category_block_id(row_index, task_id)
        state[category_block_id] = {
            "category_select": {"selected_option": {"value": category_id}}
        }
    if entry.activity:
        state[accomplishment_block_id] = {
            "accomplishment_input": {"value": entry.activity}
        }
    return state


def _state_value(state: dict | None, block_id: str, action_id: str, field: str = "value"):
    if not state:
        return None
    block = state.get(block_id, {}).get(action_id, {})
    if field == "value":
        return block.get("value")
    if field == "selected_option":
        selected = block.get("selected_option")
        if not selected:
            return None
        return selected.get("value")
    return None


def _state_value_by_prefix(
    state: dict | None, prefix: str, action_id: str, field: str = "value"
):
    """Read a value from block_id == prefix or block_id starting with prefix__."""
    if not state:
        return None
    for block_id in state:
        if block_id == prefix or block_id.startswith(prefix + "__"):
            val = _state_value(state, block_id, action_id, field)
            if val is not None:
                return val
    return None


def _pop_blocks_with_prefix(state: dict, prefix: str) -> None:
    for key in list(state.keys()):
        if key == prefix or key.startswith(prefix + "__"):
            state.pop(key, None)


def _block_token(value: str | None) -> str:
    """Stable, Slack-safe token so dependent block_ids change when parent changes."""
    raw = (value or "none").strip() or "none"
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    return (cleaned[:80] or "none")


def _duration_block_id(row_index: int) -> str:
    return f"entry_{row_index}_duration_block"


def _project_block_id(row_index: int) -> str:
    return f"entry_{row_index}_project_block"


def _task_block_id(row_index: int, project_key: str | None = None) -> str:
    # Include project so Slack treats Task as a new block when project changes
    # (otherwise the dropdown options often do not refresh).
    return f"entry_{row_index}_task_block__{_block_token(project_key)}"


def _category_block_id(row_index: int, task_key: str | None = None) -> str:
    return f"entry_{row_index}_category_block__{_block_token(task_key)}"


def _accomplishment_block_id(row_index: int) -> str:
    return f"entry_{row_index}_accomplishment_block"


def _hours_to_option_value(hours: float) -> str:
    for opt in ENTRY_DURATION_OPTIONS:
        try:
            if abs(float(opt["value"]) - float(hours)) < 1e-9:
                return opt["value"]
        except (TypeError, ValueError):
            continue
    return DEFAULT_ENTRY_DURATION


def _entry_block_ids(
    row_index: int,
    *,
    project_key: str | None = None,
    task_key: str | None = None,
) -> tuple[str, str, str, str]:
    """Return (project_block_id, task_block_id, category_block_id, accomplishment_block_id)."""
    return (
        _project_block_id(row_index),
        _task_block_id(row_index, project_key),
        _category_block_id(row_index, task_key),
        _accomplishment_block_id(row_index),
    )


def _read_row_project(state: dict | None, row_index: int) -> str | None:
    return _state_value(
        state, _project_block_id(row_index), "project_select", "selected_option"
    )


def _read_row_task(state: dict | None, row_index: int, project_key: str | None = None) -> str | None:
    if project_key is None:
        project_key = _read_row_project(state, row_index)
    val = _state_value(
        state,
        _task_block_id(row_index, project_key),
        "task_select",
        "selected_option",
    )
    if val is not None:
        return val
    return _state_value_by_prefix(
        state, f"entry_{row_index}_task_block", "task_select", "selected_option"
    )


def _read_row_category(
    state: dict | None, row_index: int, task_key: str | None = None
) -> str | None:
    if task_key is None:
        task_key = _read_row_task(state, row_index)
    val = _state_value(
        state,
        _category_block_id(row_index, task_key),
        "category_select",
        "selected_option",
    )
    if val is not None:
        return val
    return _state_value_by_prefix(
        state, f"entry_{row_index}_category_block", "category_select", "selected_option"
    )


def _row_count_from_view_state(state: dict | None) -> int:
    """How many entry slots are present in the current modal state."""
    if not state:
        return MIN_ENTRY_ROWS
    count = 0
    while count < MAX_ENTRY_ROWS:
        if (
            _duration_block_id(count) in state
            or _project_block_id(count) in state
            or _accomplishment_block_id(count) in state
            or any(str(k).startswith(f"entry_{count}_") for k in state)
        ):
            count += 1
        else:
            break
    return max(count, MIN_ENTRY_ROWS)


def _sanitize_task_selections(state: dict | None) -> dict | None:
    """
    Drop task/category selections that don't belong to the row's project/task,
    and rewrite them under the project/task-scoped block_ids so rebuilds refresh
    the cascading dropdowns in Slack.
    """
    if not state:
        return state
    for row_index in range(MAX_ENTRY_ROWS):
        project_key = _read_row_project(state, row_index)
        task_prefix = f"entry_{row_index}_task_block"
        cat_prefix = f"entry_{row_index}_category_block"
        has_row = (
            _project_block_id(row_index) in state
            or any(
                k == task_prefix or k.startswith(task_prefix + "__") or
                k == cat_prefix or k.startswith(cat_prefix + "__")
                for k in state
            )
        )
        if not has_row and _duration_block_id(row_index) not in state:
            continue

        task_key = _state_value_by_prefix(
            state, task_prefix, "task_select", "selected_option"
        )
        category_key = _state_value_by_prefix(
            state, cat_prefix, "category_select", "selected_option"
        )

        _pop_blocks_with_prefix(state, task_prefix)
        _pop_blocks_with_prefix(state, cat_prefix)

        if task_key in PLACEHOLDER_TASK_VALUES:
            task_key = None
        if task_key:
            valid_ids = {opt["value"] for opt in _task_options(project_key)}
            if task_key not in valid_ids:
                task_key = None
        if task_key:
            state[_task_block_id(row_index, project_key)] = {
                "task_select": {"selected_option": {"value": task_key}}
            }

        if category_key in PLACEHOLDER_CATEGORY_VALUES:
            category_key = None
        if category_key and task_key:
            valid_cats = {opt["value"] for opt in _category_options(task_key)}
            if category_key not in valid_cats:
                category_key = None
        if category_key and task_key:
            state[_category_block_id(row_index, task_key)] = {
                "category_select": {"selected_option": {"value": category_key}}
            }
    return state


def _entry_row_blocks(
    row_index: int,
    preserve_state: dict | None = None,
) -> list[dict]:
    saved_project = _read_row_project(preserve_state, row_index)
    saved_task = _read_row_task(preserve_state, row_index, saved_project)
    if saved_task in PLACEHOLDER_TASK_VALUES:
        saved_task = None
    saved_category = _read_row_category(preserve_state, row_index, saved_task)
    if saved_category in PLACEHOLDER_CATEGORY_VALUES:
        saved_category = None

    (
        project_block_id,
        task_block_id,
        category_block_id,
        accomplishment_block_id,
    ) = _entry_block_ids(
        row_index, project_key=saved_project, task_key=saved_task
    )
    duration_block_id = _duration_block_id(row_index)
    project_options = _project_options(allow_break=True)
    task_options = _task_options(saved_project)
    category_options = _category_options(saved_task)

    saved_duration = (
        _state_value(preserve_state, duration_block_id, "entry_duration_select", "selected_option")
        or DEFAULT_ENTRY_DURATION
    )
    if saved_duration not in ENTRY_DURATION_VALUES:
        saved_duration = DEFAULT_ENTRY_DURATION
    duration_option = next(
        opt for opt in ENTRY_DURATION_OPTIONS if opt["value"] == saved_duration
    )

    duration_element = {
        "type": "static_select",
        "action_id": "entry_duration_select",
        "initial_option": duration_option,
        "options": ENTRY_DURATION_OPTIONS,
    }
    project_element = {
        "type": "static_select",
        "action_id": "project_select",
        "placeholder": {"type": "plain_text", "text": "Select a Project"},
        "options": project_options,
    }
    task_element = {
        "type": "static_select",
        "action_id": "task_select",
        "placeholder": {"type": "plain_text", "text": "Select a Task"},
        "options": task_options,
    }
    category_element = {
        "type": "static_select",
        "action_id": "category_select",
        "placeholder": {"type": "plain_text", "text": "Select a Category"},
        "options": category_options,
    }
    accomplishment_element = {
        "type": "plain_text_input",
        "action_id": "accomplishment_input",
        "multiline": True,
        "placeholder": {"type": "plain_text", "text": "What did you accomplish?"},
    }

    saved_accomplishment = _state_value(
        preserve_state, accomplishment_block_id, "accomplishment_input"
    )

    if saved_project:
        matched = next((opt for opt in project_options if opt["value"] == saved_project), None)
        if matched:
            project_element["initial_option"] = matched
    if saved_task:
        matched_task = next((opt for opt in task_options if opt["value"] == saved_task), None)
        if matched_task:
            task_element["initial_option"] = matched_task
    if saved_category:
        matched_cat = next(
            (opt for opt in category_options if opt["value"] == saved_category), None
        )
        if matched_cat:
            category_element["initial_option"] = matched_cat
    if saved_accomplishment:
        accomplishment_element["initial_value"] = saved_accomplishment

    row_label = f"Entry {row_index + 1}"
    blocks: list[dict] = [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{row_label}*"},
        },
        {
            "type": "actions",
            "block_id": duration_block_id,
            "elements": [duration_element],
        },
        {
            "type": "actions",
            "block_id": project_block_id,
            "elements": [
                project_element,
                _create_plus_button(
                    "create_project_btn",
                    str(row_index),
                    "Create a new project",
                ),
            ],
        },
        {
            "type": "actions",
            "block_id": task_block_id,
            "elements": [
                task_element,
                _create_plus_button(
                    "create_task_btn",
                    str(row_index),
                    "Create a new task for this project",
                ),
            ],
        },
        {
            "type": "actions",
            "block_id": category_block_id,
            "elements": [
                category_element,
                _create_plus_button(
                    "create_category_btn",
                    str(row_index),
                    "Create a new category for this task",
                ),
            ],
        },
        {
            "type": "input",
            "block_id": accomplishment_block_id,
            "element": accomplishment_element,
            "label": {"type": "plain_text", "text": "What did you accomplish?"},
            "optional": True,
        },
    ]
    return blocks


def build_logtime_modal(
    row_count: int = MIN_ENTRY_ROWS,
    preserve_state: dict | None = None,
) -> dict:
    row_count = min(MAX_ENTRY_ROWS, max(MIN_ENTRY_ROWS, int(row_count or MIN_ENTRY_ROWS)))

    blocks = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Use *+* next to Project / Task / Category to create a new one. "
                        "Changing Project refreshes Tasks; changing Task refreshes Categories. "
                        "Category is optional."
                    ),
                }
            ],
        },
    ]

    for row_index in range(row_count):
        blocks.extend(_entry_row_blocks(row_index, preserve_state))

    if row_count < MAX_ENTRY_ROWS:
        blocks.append(
            {
                "type": "actions",
                "block_id": "add_entry_block",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "add_entry_btn",
                        "text": {"type": "plain_text", "text": "+ Entry"},
                        "value": "add",
                    }
                ],
            }
        )
    else:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Maximum of *{MAX_ENTRY_ROWS}* entries reached.",
                    }
                ],
            }
        )

    return {
        "type": "modal",
        "callback_id": "logtime_modal",
        "title": {"type": "plain_text", "text": "Hourly Project Update"},
        "submit": {"type": "plain_text", "text": "Submit Time & Journal"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _parse_modal_submission(state_values: dict, user_name: str) -> tuple[list[jm.LogEntry], dict, float]:
    """
    Parse modal state into journal entries.

    Returns (work_entries, errors, break_hours).
    Empty rows (no project) and Break rows are not logged.
    """
    errors = {}
    row_count = _row_count_from_view_state(state_values)
    now = datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE))
    entries = []
    break_hours = 0.0

    for row_index in range(row_count):
        duration_block_id = _duration_block_id(row_index)
        accomplishment_block_id = _accomplishment_block_id(row_index)

        hours_raw = (
            _state_value(
                state_values, duration_block_id, "entry_duration_select", "selected_option"
            )
            or DEFAULT_ENTRY_DURATION
        )
        try:
            hours = float(hours_raw)
        except (TypeError, ValueError):
            hours = 1.0

        project_key = _read_row_project(state_values, row_index)
        task_key = _read_row_task(state_values, row_index, project_key)
        category_key = _read_row_category(state_values, row_index, task_key)
        accomplishment = _state_value(state_values, accomplishment_block_id, "accomplishment_input")

        # Completely empty → treat as Break (not recorded; no break tally).
        if not project_key:
            continue

        if project_key == BREAK_PROJECT_VALUE:
            break_hours += hours
            continue

        if task_key in PLACEHOLDER_TASK_VALUES:
            task_key = None
        if category_key in PLACEHOLDER_CATEGORY_VALUES:
            category_key = None

        missing_fields = []
        if not task_key:
            missing_fields.append("task")
        # Category is optional (tasks may have none, or the user may leave it blank).
        if not accomplishment or not accomplishment.strip():
            missing_fields.append("accomplishment")
        # Slack only accepts view errors on input blocks, not actions blocks.
        if missing_fields:
            errors[accomplishment_block_id] = (
                f"Complete {' / '.join(missing_fields)} for this entry, "
                "or clear Project to leave it as Break."
            )
            continue

        entries.append(
            jm.LogEntry(
                timestamp=now,
                user=user_name,
                hours=hours,
                task=task_key,
                category=category_key or "",
                activity=accomplishment.strip(),
                project_key=project_key,
            )
        )

    if not entries and not errors:
        errors[_accomplishment_block_id(0)] = "Fill out at least one work entry before submitting."

    return entries, errors, break_hours


def _state_with_meta_fallback(state: dict | None, meta: dict | None) -> dict:
    """
    Overlay live Slack state on top of compact rows from private_metadata.

    After views.update auto-selects a newly created project/task via
    initial_option, Slack often omits that select from state.values until the
    user opens the dropdown. Metadata keeps those selections so Task/Category +
    still work in the same session.
    """
    merged = _preserve_from_snapshot((meta or {}).get("rows"))
    for block_id, actions in (state or {}).items():
        merged[block_id] = actions
    return merged


def _logtime_private_metadata(
    *,
    channel_id: str = "",
    user_id: str = "",
    row_count: int = MIN_ENTRY_ROWS,
    preserve_state: dict | None = None,
    base: dict | None = None,
) -> dict:
    """Build logtime modal private_metadata, including a compact rows snapshot."""
    row_count = min(MAX_ENTRY_ROWS, max(MIN_ENTRY_ROWS, int(row_count or MIN_ENTRY_ROWS)))
    meta = dict(base or {})
    meta["channel_id"] = channel_id or meta.get("channel_id") or ""
    meta["user_id"] = user_id or meta.get("user_id") or ""
    meta["row_count"] = row_count
    meta["rows"] = _snapshot_logtime_rows(preserve_state, row_count)
    return _trim_parent_meta_for_slack(meta)


def _rebuild_logtime_view(ack, body, client, *, force: bool = True) -> None:
    """Rebuild logtime modal from current state (cascading dropdowns / add entry)."""
    ack()
    view = body.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    state = _sanitize_task_selections(
        _state_with_meta_fallback(
            view.get("state", {}).get("values", {}) or {}, meta
        )
    )
    row_count = int(meta.get("row_count") or _row_count_from_view_state(state) or MIN_ENTRY_ROWS)
    row_count = min(MAX_ENTRY_ROWS, max(MIN_ENTRY_ROWS, row_count))
    updated = build_logtime_modal(row_count=row_count, preserve_state=state)
    updated["private_metadata"] = json.dumps(
        _logtime_private_metadata(
            channel_id=meta.get("channel_id", ""),
            user_id=meta.get("user_id", ""),
            row_count=row_count,
            preserve_state=state,
            base=meta,
        )
    )
    try:
        client.views_update(
            view_id=view.get("id"),
            hash=view.get("hash"),
            view=updated,
        )
    except Exception as exc:
        print(f"[SLACK WARNING] Could not refresh logtime modal: {exc}", flush=True)


def _parent_metadata_from_view(view: dict) -> dict:
    meta = {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    meta["parent_view_id"] = view.get("id", "")
    # Compact rows may be stored; full preserve_state is too large for Slack's 3000 cap.
    return meta

def _build_create_named_modal(kind: str, parent_meta: dict) -> dict:
    titles = {
        "task": "Create Task",
        "category": "Create Category",
        "project": "Create Project",
    }
    labels = {
        "task": "Task name",
        "category": "Category name",
        "project": "Project name",
    }
    blocks = [
        {
            "type": "input",
            "block_id": "name_block",
            "element": {
                "type": "plain_text_input",
                "action_id": "name_input",
                "placeholder": {"type": "plain_text", "text": labels[kind]},
            },
            "label": {"type": "plain_text", "text": labels[kind]},
        }
    ]
    if kind == "project":
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "After you submit, you'll get a short-lived link to choose "
                            "the project's Google Drive folder. Add tasks for the "
                            "project with *+* next to Task after selecting it."
                        ),
                    }
                ],
            }
        )
    if kind == "task":
        project_name = parent_meta.get("project_name") or parent_meta.get("project_id") or "this project"
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"This task will only appear for *{project_name}*.",
                    }
                ],
            }
        )
    if kind == "category":
        task_name = parent_meta.get("task_name") or parent_meta.get("task_id") or "this task"
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"This category will only appear for task *{task_name}*.",
                    }
                ],
            }
        )
    return {
        "type": "modal",
        "callback_id": f"create_{kind}_modal",
        "title": {"type": "plain_text", "text": titles[kind]},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(
            _trim_parent_meta_for_slack(parent_meta), separators=(",", ":")
        ),
        "blocks": blocks,
    }


def _snapshot_logtime_rows(state: dict | None, row_count: int) -> list[dict]:
    """Compact entry snapshot for create-modal private_metadata (≤3000 chars)."""
    rows: list[dict] = []
    for i in range(max(MIN_ENTRY_ROWS, int(row_count or MIN_ENTRY_ROWS))):
        activity = _state_value(state, _accomplishment_block_id(i), "accomplishment_input") or ""
        hours = (
            _state_value(
                state, _duration_block_id(i), "entry_duration_select", "selected_option"
            )
            or DEFAULT_ENTRY_DURATION
        )
        rows.append(
            {
                "p": _read_row_project(state, i) or "",
                "t": _read_row_task(state, i) or "",
                "c": _read_row_category(state, i) or "",
                "h": hours,
                "a": str(activity)[:400],
            }
        )
    return rows


def _preserve_from_snapshot(rows: list | None) -> dict:
    """Rebuild Slack-shaped preserve_state from a compact snapshot."""
    state: dict = {}
    if not isinstance(rows, list):
        return state
    for i, row in enumerate(rows[:MAX_ENTRY_ROWS]):
        if not isinstance(row, dict):
            continue
        project_key = str(row.get("p") or "")
        task_key = str(row.get("t") or "")
        category_key = str(row.get("c") or "")
        hours = str(row.get("h") or DEFAULT_ENTRY_DURATION)
        activity = str(row.get("a") or "")
        if hours not in ENTRY_DURATION_VALUES:
            hours = DEFAULT_ENTRY_DURATION
        if task_key in PLACEHOLDER_TASK_VALUES:
            task_key = ""
        if category_key in PLACEHOLDER_CATEGORY_VALUES:
            category_key = ""
        (
            project_block_id,
            task_block_id,
            category_block_id,
            accomplishment_block_id,
        ) = _entry_block_ids(
            i, project_key=project_key or None, task_key=task_key or None
        )
        state[_duration_block_id(i)] = {
            "entry_duration_select": {"selected_option": {"value": hours}}
        }
        if project_key:
            state[project_block_id] = {
                "project_select": {"selected_option": {"value": project_key}}
            }
        if task_key:
            state[task_block_id] = {
                "task_select": {"selected_option": {"value": task_key}}
            }
        if category_key and task_key:
            state[category_block_id] = {
                "category_select": {"selected_option": {"value": category_key}}
            }
        if activity:
            state[accomplishment_block_id] = {
                "accomplishment_input": {"value": activity}
            }
    return state


def _apply_created_selection(
    preserve: dict,
    *,
    row_index: int,
    project_id: str = "",
    task_id: str = "",
    select_task_id: str | None = None,
    select_category_id: str | None = None,
    select_project_id: str | None = None,
) -> dict:
    """Force-select a newly created project/task/category on the active entry row."""
    row_index = max(0, min(MAX_ENTRY_ROWS - 1, int(row_index or 0)))
    project_id = (project_id or "").strip()
    task_id = (task_id or "").strip()

    if select_project_id:
        project_id = select_project_id
        preserve[_project_block_id(row_index)] = {
            "project_select": {"selected_option": {"value": select_project_id}}
        }
        _pop_blocks_with_prefix(preserve, f"entry_{row_index}_task_block")
        _pop_blocks_with_prefix(preserve, f"entry_{row_index}_category_block")

    if select_task_id:
        if not project_id:
            project_id = _read_row_project(preserve, row_index) or ""
        _pop_blocks_with_prefix(preserve, f"entry_{row_index}_task_block")
        _pop_blocks_with_prefix(preserve, f"entry_{row_index}_category_block")
        preserve[_task_block_id(row_index, project_id or None)] = {
            "task_select": {"selected_option": {"value": select_task_id}}
        }
        task_id = select_task_id

    if select_category_id:
        if not task_id:
            task_id = _read_row_task(preserve, row_index, project_id or None) or ""
        if not project_id:
            project_id = _read_row_project(preserve, row_index) or ""
        _pop_blocks_with_prefix(preserve, f"entry_{row_index}_category_block")
        if task_id:
            if project_id:
                preserve[_task_block_id(row_index, project_id)] = {
                    "task_select": {"selected_option": {"value": task_id}}
                }
            preserve[_category_block_id(row_index, task_id)] = {
                "category_select": {"selected_option": {"value": select_category_id}}
            }
    return preserve


def _trim_parent_meta_for_slack(meta: dict) -> dict:
    """Keep create-modal private_metadata under Slack's 3000-character limit."""
    payload = dict(meta)
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded) <= 3000:
        return payload
    rows = payload.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                row["a"] = ""
        encoded = json.dumps(payload, separators=(",", ":"))
        if len(encoded) <= 3000:
            return payload
        row_index = int(payload.get("row_index") or 0)
        trimmed = [
            {"p": "", "t": "", "c": "", "h": DEFAULT_ENTRY_DURATION, "a": ""}
            for _ in rows
        ]
        if 0 <= row_index < len(rows) and isinstance(rows[row_index], dict):
            keep = dict(rows[row_index])
            keep["a"] = ""
            trimmed[row_index] = keep
        payload["rows"] = trimmed
    return payload


def _enrich_create_parent_meta(
    parent_meta: dict,
    *,
    view: dict,
    state: dict | None,
    row_index: int,
) -> dict:
    """Attach channel/user/row snapshot used to restore + auto-select after create."""
    root_meta: dict = {}
    try:
        root_meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        root_meta = {}
    parent_meta["channel_id"] = root_meta.get("channel_id") or parent_meta.get("channel_id") or ""
    parent_meta["user_id"] = (
        root_meta.get("user_id") or parent_meta.get("user_id") or ""
    )
    parent_meta["row_index"] = int(row_index or 0)
    merged_state = _state_with_meta_fallback(state, root_meta)
    row_count = int(
        root_meta.get("row_count")
        or _row_count_from_view_state(merged_state)
        or MIN_ENTRY_ROWS
    )
    parent_meta["row_count"] = min(MAX_ENTRY_ROWS, max(MIN_ENTRY_ROWS, row_count))
    parent_meta["rows"] = _snapshot_logtime_rows(merged_state, parent_meta["row_count"])
    return parent_meta


def _refresh_parent_logtime(
    client,
    parent_meta: dict,
    *,
    select_task_id: str | None = None,
    select_category_id: str | None = None,
    select_project_id: str | None = None,
) -> None:
    view_id = parent_meta.get("parent_view_id")
    if not view_id:
        return
    row_count = int(parent_meta.get("row_count") or MIN_ENTRY_ROWS)
    row_count = min(MAX_ENTRY_ROWS, max(MIN_ENTRY_ROWS, row_count))
    preserve = _preserve_from_snapshot(parent_meta.get("rows"))
    _apply_created_selection(
        preserve,
        row_index=int(parent_meta.get("row_index") or 0),
        project_id=str(parent_meta.get("project_id") or ""),
        task_id=str(parent_meta.get("task_id") or ""),
        select_task_id=select_task_id,
        select_category_id=select_category_id,
        select_project_id=select_project_id,
    )
    updated = build_logtime_modal(row_count=row_count, preserve_state=preserve)
    # Persist auto-selected project/task/category in private_metadata so the next
    # "+" click can see them even when Slack omits initial_option from state.
    updated["private_metadata"] = json.dumps(
        _logtime_private_metadata(
            channel_id=parent_meta.get("channel_id", ""),
            user_id=parent_meta.get("user_id", ""),
            row_count=row_count,
            preserve_state=preserve,
        )
    )
    try:
        client.views_update(view_id=view_id, view=updated)
    except Exception as exc:
        print(f"[SLACK WARNING] Could not refresh logtime modal: {exc}", flush=True)


@app.command("/refreshjournal")
def handle_refresh_journal(ack, body, client):
    ack()
    project_key = (body.get("text") or "").strip()
    user_id = body.get("user_id")

    if not project_key:
        client.chat_postEphemeral(
            channel=body["channel_id"],
            user=user_id,
            text=(
                "Usage: `/refreshjournal <project_key>`\n"
                "Examples: `tahoe_backyard`, `wood_energy_facility`, `8494_speckled`"
            ),
        )
        return

    assets = project_router.ensure_project_assets(project_key)
    project_name = project_router.get_project_display_name(project_key)
    if not assets:
        client.chat_postEphemeral(
            channel=body["channel_id"],
            user=user_id,
            text=(
                f"No Drive folder mapped for `{project_key}`. "
                "Set the project's `drive_folder_url` in Firestore "
                "(or PROJECT_FOLDER_* / PROJECT_DOC_MAP as fallback)."
            ),
        )
        return

    result = agent_journal.refresh_weekly_summary(
        assets.document_id,
        project_name,
        project_key=project_key,
        spreadsheet_id=assets.spreadsheet_id,
    )
    if result.summary_updated:
        refresh_note = " Doc tables refreshed." if result.docs_refreshed else ""
        sheet_note = f" Sheet `{result.spreadsheet_id}`." if result.spreadsheet_id else ""
        text = f"✅ Refreshed weekly summary for *{project_name}*.{sheet_note}{refresh_note}"
    else:
        text = f"❌ Could not refresh summary for *{project_name}*: {result.error_message or 'unknown error'}"

    try:
        client.chat_postEphemeral(channel=body["channel_id"], user=user_id, text=text)
    except Exception:
        client.chat_postMessage(channel=user_id, text=text)


@app.command("/logtime")
def handle_logtime_command(ack, body, client):
    ack()
    print(f"[SLACK SOCKET] Received /logtime command from {body.get('user_name')}")
    _open_logtime_modal(
        client,
        trigger_id=body["trigger_id"],
        channel_id=body.get("channel_id", ""),
        user_id=body.get("user_id", ""),
    )


@app.command("/myslackid")
def handle_myslackid_command(ack, body, client):
    ack()
    user_id = body.get("user_id", "")
    client.chat_postEphemeral(
        channel=body["channel_id"],
        user=user_id,
        text=(
            f"Your Slack user ID is `{user_id}`.\n"
            "Set `REMINDER_USER_ID` to this value in env.yaml, then redeploy."
        ),
    )


@app.command("/testreminder")
def handle_test_reminder_command(ack, body, client):
    """Send the hourly Block Kit message now."""
    ack()
    user_id = body.get("user_id", "")
    if hourly_reminder.send_hourly_reminder(client, user_id):
        client.chat_postEphemeral(
            channel=body["channel_id"],
            user=user_id,
            text="Hourly block message sent to your DM. Click *Open Time Entry Form*.",
        )
    else:
        client.chat_postEphemeral(
            channel=body["channel_id"],
            user=user_id,
            text="Failed to send reminder. Check Cloud Run logs.",
        )


@app.action("open_logtime_modal")
def handle_open_logtime_modal(ack, body, client):
    ack()
    trigger_id = body.get("trigger_id")
    user_id = body.get("user", {}).get("id", "")
    channel_id = body.get("channel", {}).get("id", user_id)
    if not trigger_id:
        print("[SLACK ERROR] Missing trigger_id for open_logtime_modal.")
        return
    print(f"[SLACK SOCKET] Opening logtime modal for {user_id}.")
    _open_logtime_modal(client, trigger_id, channel_id, user_id)


@app.action("project_select")
def handle_project_select(ack, body, client):
    # Rebuild so the Task dropdown switches to this project's tasks.
    _rebuild_logtime_view(ack, body, client, force=True)


@app.action("task_select")
def handle_task_select(ack, body, client):
    # Rebuild so the Category dropdown switches to this task's categories.
    _rebuild_logtime_view(ack, body, client, force=True)


@app.action("category_select")
def handle_category_select(ack, body, client):
    ack()


@app.action("entry_duration_select")
def handle_entry_duration_select(ack, body, client):
    ack()


@app.action("add_entry_btn")
def handle_add_entry_btn(ack, body, client):
    ack()
    view = body.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    state = _sanitize_task_selections(
        _state_with_meta_fallback(
            view.get("state", {}).get("values", {}) or {}, meta
        )
    )
    current = int(meta.get("row_count") or _row_count_from_view_state(state) or MIN_ENTRY_ROWS)
    row_count = min(MAX_ENTRY_ROWS, max(MIN_ENTRY_ROWS, current + 1))
    updated = build_logtime_modal(row_count=row_count, preserve_state=state)
    updated["private_metadata"] = json.dumps(
        _logtime_private_metadata(
            channel_id=meta.get("channel_id", ""),
            user_id=meta.get("user_id", ""),
            row_count=row_count,
            preserve_state=state,
            base=meta,
        )
    )
    try:
        client.views_update(
            view_id=view.get("id"),
            hash=view.get("hash"),
            view=updated,
        )
    except Exception as exc:
        print(f"[SLACK WARNING] Could not add entry row: {exc}", flush=True)


def _push_create_modal(ack, body, client, kind: str):
    ack()
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return
    view = body.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    state = _state_with_meta_fallback(
        view.get("state", {}).get("values", {}) or {}, meta
    )
    actions = body.get("actions") or []
    try:
        row_index = int((actions[0] or {}).get("value") or 0)
    except (TypeError, ValueError, IndexError):
        row_index = 0
    parent_meta = _parent_metadata_from_view(view)
    parent_meta["user_id"] = body.get("user", {}).get("id", "")
    _enrich_create_parent_meta(
        parent_meta, view=view, state=state, row_index=row_index
    )
    try:
        client.views_push(
            trigger_id=trigger_id,
            view=_build_create_named_modal(kind, parent_meta),
        )
    except Exception as exc:
        print(f"[SLACK WARNING] Could not push create_{kind} modal: {exc}", flush=True)


@app.action("create_project_btn")
def handle_create_project_btn(ack, body, client):
    _push_create_modal(ack, body, client, "project")


@app.action("create_task_btn")
def handle_create_task_btn(ack, body, client):
    ack()
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return
    view = body.get("view") or {}
    actions = body.get("actions") or []
    try:
        row_index = int((actions[0] or {}).get("value") or 0)
    except (TypeError, ValueError, IndexError):
        row_index = 0
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    state = _state_with_meta_fallback(
        view.get("state", {}).get("values", {}) or {}, meta
    )
    project_id = _read_row_project(state, row_index)
    user_id = body.get("user", {}).get("id", "")

    parent_meta = _parent_metadata_from_view(view)
    parent_meta["user_id"] = user_id
    _enrich_create_parent_meta(
        parent_meta, view=view, state=state, row_index=row_index
    )
    channel_id = parent_meta.get("channel_id") or ""

    if not project_id or project_id == BREAK_PROJECT_VALUE:
        client.chat_postEphemeral(
            channel=channel_id or user_id,
            user=user_id,
            text="Select a project on that entry first, then use *+* to add a task for it.",
        )
        return

    parent_meta["project_id"] = project_id
    try:
        project = firestore_store.get_project(project_id)
        parent_meta["project_name"] = project.name if project else project_id
    except Exception:
        parent_meta["project_name"] = project_id
    try:
        client.views_push(
            trigger_id=trigger_id,
            view=_build_create_named_modal("task", parent_meta),
        )
    except Exception as exc:
        print(f"[SLACK WARNING] Could not push create_task modal: {exc}", flush=True)


@app.action("create_category_btn")
def handle_create_category_btn(ack, body, client):
    ack()
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return
    view = body.get("view") or {}
    actions = body.get("actions") or []
    try:
        row_index = int((actions[0] or {}).get("value") or 0)
    except (TypeError, ValueError, IndexError):
        row_index = 0
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    state = _state_with_meta_fallback(
        view.get("state", {}).get("values", {}) or {}, meta
    )
    project_id = _read_row_project(state, row_index)
    task_id = _read_row_task(state, row_index, project_id)
    user_id = body.get("user", {}).get("id", "")

    parent_meta = _parent_metadata_from_view(view)
    parent_meta["user_id"] = user_id
    _enrich_create_parent_meta(
        parent_meta, view=view, state=state, row_index=row_index
    )
    channel_id = parent_meta.get("channel_id") or ""

    if not task_id or task_id in PLACEHOLDER_TASK_VALUES:
        client.chat_postEphemeral(
            channel=channel_id or user_id,
            user=user_id,
            text="Select a task on that entry first, then use *+* to add a category for it.",
        )
        return

    parent_meta["project_id"] = project_id or ""
    parent_meta["task_id"] = task_id
    try:
        task = firestore_store.get_task(task_id)
        parent_meta["task_name"] = task.name if task else task_id
    except Exception:
        parent_meta["task_name"] = task_id
    try:
        client.views_push(
            trigger_id=trigger_id,
            view=_build_create_named_modal("category", parent_meta),
        )
    except Exception as exc:
        print(f"[SLACK WARNING] Could not push create_category modal: {exc}", flush=True)


@app.view("create_task_modal")
def handle_create_task_modal(ack, body, client, view):
    name = (_state_value(view.get("state", {}).get("values", {}), "name_block", "name_input") or "").strip()
    parent_meta = json.loads(view.get("private_metadata") or "{}")
    project_id = (parent_meta.get("project_id") or "").strip()
    if not name:
        ack(response_action="errors", errors={"name_block": "Enter a task name."})
        return
    if not project_id:
        ack(
            response_action="errors",
            errors={"name_block": "Select a project on the entry, then create the task again."},
        )
        return
    try:
        created = firestore_store.create_task(name, project_id)
    except Exception as exc:
        ack(response_action="errors", errors={"name_block": f"Could not create task: {exc}"})
        return
    ack()
    _refresh_parent_logtime(client, parent_meta, select_task_id=created.id)
    user_id = body.get("user", {}).get("id") or parent_meta.get("user_id")
    project_label = parent_meta.get("project_name") or project_id
    if user_id:
        client.chat_postEphemeral(
            channel=parent_meta.get("channel_id") or user_id,
            user=user_id,
            text=(
                f"✅ Created task *{created.name}* for *{project_label}* "
                "and selected it on that entry."
            ),
        )


@app.view("create_category_modal")
def handle_create_category_modal(ack, body, client, view):
    name = (_state_value(view.get("state", {}).get("values", {}), "name_block", "name_input") or "").strip()
    parent_meta = json.loads(view.get("private_metadata") or "{}")
    task_id = (parent_meta.get("task_id") or "").strip()
    if not name:
        ack(response_action="errors", errors={"name_block": "Enter a category name."})
        return
    if not task_id:
        ack(
            response_action="errors",
            errors={
                "name_block": "Select a task on the entry, then create the category again."
            },
        )
        return
    try:
        created = firestore_store.create_category(name, task_id)
    except Exception as exc:
        ack(response_action="errors", errors={"name_block": f"Could not create category: {exc}"})
        return
    ack()
    _refresh_parent_logtime(client, parent_meta, select_category_id=created.id)
    user_id = body.get("user", {}).get("id") or parent_meta.get("user_id")
    task_label = parent_meta.get("task_name") or task_id
    if user_id:
        client.chat_postEphemeral(
            channel=parent_meta.get("channel_id") or user_id,
            user=user_id,
            text=(
                f"✅ Created category *{created.name}* for task *{task_label}* "
                "and selected it on that entry."
            ),
        )


@app.view("create_project_modal")
def handle_create_project_modal(ack, body, client, view):
    name = (_state_value(view.get("state", {}).get("values", {}), "name_block", "name_input") or "").strip()
    if not name:
        ack(response_action="errors", errors={"name_block": "Enter a project name."})
        return
    try:
        created = firestore_store.create_project(name)
    except Exception as exc:
        ack(response_action="errors", errors={"name_block": f"Could not create project: {exc}"})
        return

    user_id = body.get("user", {}).get("id", "")
    token = drive_picker.create_picker_token(created.id, created.name, user_id)
    link = drive_picker.picker_url(token)
    parent_meta = json.loads(view.get("private_metadata") or "{}")

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ Created *{created.name}* (`{created.id}`).\n"
                    "Next: choose its Google Drive folder (link expires in 15 minutes)."
                ),
            },
        }
    ]
    if link.startswith("http"):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Choose Drive folder"},
                        "url": link,
                        "action_id": "open_drive_picker_link",
                    }
                ],
            }
        )
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Or open: `{link}`"}],
            }
        )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Set `SERVICE_PUBLIC_URL` to this Cloud Run service URL, then "
                        "create the project again so the folder-picker button works.\n"
                        f"Relative path would be: `{link}`"
                    ),
                },
            }
        )

    ack(
        response_action="update",
        view={
            "type": "modal",
            "title": {"type": "plain_text", "text": "Project created"},
            "close": {"type": "plain_text", "text": "Done"},
            "blocks": blocks,
        },
    )
    _refresh_parent_logtime(client, parent_meta, select_project_id=created.id)


@app.action("open_drive_picker_link")
def handle_open_drive_picker_link(ack):
    ack()


def _slack_employee_identity(client, user_id: str) -> tuple[str, str]:
    """
    Return (display_name, last_name). Prefer Firestore User.display_name.
    """
    fs_name = employee_router.get_employee_display_name(user_id)
    display = fs_name or "Employee"
    last_name = "Employee"
    try:
        info = client.users_info(user=user_id)
        user_obj = (info or {}).get("user") or {}
        profile = user_obj.get("profile") or {}
        if not fs_name:
            display = (
                profile.get("real_name")
                or user_obj.get("real_name")
                or profile.get("display_name")
                or user_obj.get("name")
                or display
            )
        last_name = (profile.get("last_name") or "").strip()
        if not last_name:
            parts = str(display).strip().split()
            last_name = parts[-1] if parts else "Employee"
    except Exception as exc:
        print(f"[SLACK WARNING] Could not resolve Slack profile for {user_id}: {exc}")
        if not fs_name:
            parts = str(display).strip().split()
            last_name = parts[-1] if parts else "Employee"
    return display, last_name


@app.view("logtime_modal")
def handle_logtime_submission(ack, body, client, view):
    user = body.get("user", {})
    slack_username = user.get("name") or user.get("username", "Unknown User")
    state_values = view.get("state", {}).get("values", {})

    # Temporary label for validation; replaced with Firestore/Slack display name below
    # so Activity Log matches seeded rows (display name) instead of Slack username.
    entries, errors, break_hours = _parse_modal_submission(state_values, slack_username)
    if errors:
        ack(response_action="errors", errors=errors)
        return

    ack()

    metadata = json.loads(view.get("private_metadata") or "{}")
    channel_id = metadata.get("channel_id")
    user_id = user.get("id") or metadata.get("user_id")
    display_name, _last_name = (
        _slack_employee_identity(client, user_id) if user_id else (slack_username, "Employee")
    )
    for entry in entries:
        entry.user = display_name

    duration = (
        _hours_to_option_value(entries[0].hours) if entries else DEFAULT_ENTRY_DURATION
    )

    entries_by_project: dict[str, list[jm.LogEntry]] = defaultdict(list)
    for entry in entries:
        entries_by_project[entry.project_key].append(entry)

    if entries and user_id:
        try:
            # Prefill memory is Entry 1 only — later entries are not reused next time.
            first = entries[0]
            firestore_store.set_last_logtime(
                user_id,
                duration,
                [
                    firestore_store.LastLogEntry(
                        project_key=first.project_key,
                        task=first.task,
                        category=first.category,
                        activity=first.activity,
                        hours=_hours_to_option_value(first.hours),
                    )
                ],
            )
            print(
                f"[FIRESTORE] Saved last_logtime for {user_id}: Entry 1 only",
                flush=True,
            )
        except Exception as exc:
            print(f"[SLACK WARNING] Could not save last logtime for {user_id}: {exc}", flush=True)

    user_rate = firestore_store.DEFAULT_USER_RATE
    if user_id:
        try:
            user_rec = firestore_store.get_user(user_id)
            if user_rec:
                user_rate = user_rec.rate
            else:
                # First logtime for an unknown user — create with default rate.
                firestore_store.upsert_user(
                    slack_user_id=user_id,
                    display_name=display_name,
                    rate=firestore_store.DEFAULT_USER_RATE,
                )
                user_rate = firestore_store.DEFAULT_USER_RATE
        except Exception as exc:
            print(f"[SLACK WARNING] Could not load user rate for {user_id}: {exc}", flush=True)

    total_hours = round(sum(entry.hours for entry in entries), 2)
    print(
        f"[SLACK SOCKET] Modal submitted by {display_name} ({slack_username}): "
        f"{len(entries)} journal entries ({total_hours:g} hr), "
        f"break={break_hours:g} hr, rate={user_rate}"
    )

    # Firestore first: every Slack work entry → time_logs + Task.completed
    if entries and user_id:
        try:
            notes = firestore_store.apply_logtime_billing(
                user_id=user_id, entries=entries
            )
            for note in notes:
                print(f"[FIRESTORE] {note}", flush=True)
            print(
                f"[FIRESTORE] Permanent activity stored in time_logs + "
                f"users/{user_id}/activities ({len(entries)} new row(s)). "
                f"last_logtime.prefill_rows is form prefill only.",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[FIRESTORE WARNING] time_logs / Completed update failed: {exc}",
                flush=True,
            )

    results = []
    # Doc/chart sync is deferred until after timesheet so an OOM during chart
    # embed cannot drop timesheet hours.
    pending_doc_syncs: list[tuple[str, str, str, str, str]] = []
    for project_key, project_entries in entries_by_project.items():
        project_name = project_router.get_project_display_name(project_key)
        try:
            assets = project_router.ensure_project_assets(project_key)
        except Exception as exc:
            results.append(
                f"❌ *{project_name}* (`{project_key}`): Drive setup failed — {exc}"
            )
            continue
        if not assets:
            results.append(
                f"⚠️ No Drive folder mapped for key `{project_key}` "
                f"(display '{project_name}'). "
                "Set `drive_folder_url` in Firestore (or PROJECT_FOLDER_* fallback)."
            )
            continue

        result = agent_journal.process_journal_update(
            assets.document_id,
            project_name,
            project_entries,
            project_key=project_key,
            spreadsheet_id=assets.spreadsheet_id,
            rate=user_rate,
            sync_document=False,
        )
        project_hours = round(sum(entry.hours for entry in project_entries), 2)
        if result.log_appended:
            pending_doc_syncs.append(
                (
                    assets.document_id,
                    project_name,
                    result.spreadsheet_id or assets.spreadsheet_id,
                    project_key,
                    assets.document_title,
                )
            )
            results.append(
                f"✅ *{project_name}* (Doc `{assets.document_title}`): "
                f"logged {project_hours:g} hr(s) to Sheet. "
                "Doc/chart sync running in the background."
            )
        else:
            results.append(
                f"❌ *{project_name}*: could not write journal "
                f"({result.error_message or 'unknown error'})."
            )

    if break_hours:
        results.append(
            f"☕ Break: {break_hours:g} hr skipped (not written to the detailed activity log)."
        )

    if entries and user_id:
        display_name, _last_name = _slack_employee_identity(client, user_id)
        ts_result = agent_timesheets.process_timesheet_update(
            slack_user_id=user_id,
            employee_display_name=display_name,
            entries=entries,
        )
        if ts_result.success:
            results.append(
                f"✅ Timesheet `{ts_result.spreadsheet_title}`: "
                f"updated {ts_result.rows_touched} entr(y/ies)."
            )
        else:
            results.append(
                f"⚠️ Timesheet write failed: {ts_result.error_message or 'unknown error'}."
            )

    for document_id, project_name, spreadsheet_id, project_key, _doc_title in pending_doc_syncs:
        agent_journal.schedule_period_document_sync(
            document_id,
            project_name,
            spreadsheet_id,
            project_key=project_key,
        )

    message = "\n".join(results) if results else "No entries were processed."
    if user_id:
        try:
            if channel_id:
                client.chat_postEphemeral(channel=channel_id, user=user_id, text=message)
            else:
                client.chat_postMessage(channel=user_id, text=message)
        except Exception as exc:
            print(f"[SLACK WARNING] Could not send confirmation message: {exc}")


class HealthAndPickerServer(BaseHTTPRequestHandler):
    def _maybe_remember_public_url(self):
        """Learn Cloud Run public URL from inbound request Host header."""
        if drive_picker.public_base_url():
            return
        host = (self.headers.get("Host") or "").strip()
        if not host or host.startswith("localhost") or host.startswith("127."):
            return
        scheme = (
            "https"
            if "run.app" in host or self.headers.get("X-Forwarded-Proto") == "https"
            else "http"
        )
        os.environ["SERVICE_PUBLIC_URL"] = f"{scheme}://{host}"
        print(f"[PICKER] Learned SERVICE_PUBLIC_URL={os.environ['SERVICE_PUBLIC_URL']}", flush=True)

    def do_GET(self):
        self._maybe_remember_public_url()
        parsed = urlparse(self.path)
        if parsed.path.startswith("/drive-picker/"):
            token = parsed.path.split("/drive-picker/", 1)[1].strip("/")
            status, content_type, body = drive_picker.render_picker_page(token)
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self):
        self._maybe_remember_public_url()
        parsed = urlparse(self.path)
        if parsed.path.startswith("/drive-picker/"):
            token = parsed.path.split("/drive-picker/", 1)[1].strip("/")
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            status, content_type, body = drive_picker.handle_picker_post(
                token, raw, self.headers.get("Content-Type", "")
            )
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthAndPickerServer)
    print(f"Health + Drive picker server listening on port {port}...")
    server.serve_forever()


if __name__ == "__main__":
    try:
        firestore_store.seed_if_empty()
    except Exception as exc:
        print(f"[FIRESTORE] Seed skipped / failed (env fallbacks still active): {exc}", flush=True)

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        print("[CRITICAL] SLACK_APP_TOKEN is missing. Outbound socket connection cannot start.")
    else:
        hourly_reminder.start_hourly_reminder_thread(app.client)
        print("Bolt app is running in Socket Mode! Listening for Slack events...")
        handler = SocketModeHandler(app, app_token)
        handler.start()
