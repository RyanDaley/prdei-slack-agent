import json
import os
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

FALLBACK_CATEGORY_OPTIONS = [
    {"text": {"type": "plain_text", "text": "CAD / BIM Modeling"}, "value": "cad_modeling"},
    {"text": {"type": "plain_text", "text": "Permitting / Code Review"}, "value": "permitting"},
    {"text": {"type": "plain_text", "text": "Engineering / Calcs"}, "value": "engineering"},
]

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
MIN_ENTRY_ROWS = 2
MAX_ENTRY_ROWS = 6


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


def _category_options() -> list[dict]:
    try:
        categories = firestore_store.list_categories()
        if categories:
            return [_option(c.name, c.id) for c in categories]
    except Exception as exc:
        print(f"[SLACK] Firestore category options failed; using fallback: {exc}", flush=True)
    return list(FALLBACK_CATEGORY_OPTIONS)


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
    row_count = MIN_ENTRY_ROWS
    try:
        last = firestore_store.get_last_logtime(user_id)
        if last and last.entries:
            preserve_state = _preserve_state_from_last_logtime(last)
            row_count = _row_count_for_state(preserve_state)
            print(
                f"[SLACK] Prefilling logtime for {user_id} "
                f"from last entry ({len(last.entries)} row(s), {row_count} slots).",
                flush=True,
            )
    except Exception as exc:
        print(f"[SLACK WARNING] Could not load last logtime for {user_id}: {exc}", flush=True)

    modal = build_logtime_modal(row_count=row_count, preserve_state=preserve_state)
    modal["private_metadata"] = json.dumps(
        {"channel_id": channel_id, "user_id": user_id, "row_count": row_count}
    )
    client.views_open(trigger_id=trigger_id, view=modal)


def _preserve_state_from_last_logtime(last: firestore_store.LastLogtime) -> dict:
    """
    Build Slack-shaped preserve_state so build_logtime_modal can set
    initial_option / initial_value from the previous submission.
    """
    state: dict = {}
    for row_index, entry in enumerate(last.entries[:MAX_ENTRY_ROWS]):
        (
            project_block_id,
            task_block_id,
            category_block_id,
            accomplishment_block_id,
        ) = _entry_block_ids(row_index)
        duration_block_id = _duration_block_id(row_index)
        hours = entry.hours if entry.hours in ENTRY_DURATION_VALUES else DEFAULT_ENTRY_DURATION
        state[duration_block_id] = {
            "entry_duration_select": {"selected_option": {"value": hours}}
        }
        if entry.project_key:
            state[project_block_id] = {
                "project_select": {"selected_option": {"value": entry.project_key}}
            }
        if entry.task:
            task_id = entry.task
            # Legacy global task keys → project-scoped document ids for prefill.
            if (
                entry.project_key
                and entry.project_key != BREAK_PROJECT_VALUE
                and "__" not in task_id
            ):
                task_id = firestore_store.task_doc_id(entry.project_key, task_id)
            state[task_block_id] = {
                "task_select": {"selected_option": {"value": task_id}}
            }
        if entry.category:
            state[category_block_id] = {
                "category_select": {"selected_option": {"value": entry.category}}
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


def _duration_block_id(row_index: int) -> str:
    return f"entry_{row_index}_duration_block"


def _hours_to_option_value(hours: float) -> str:
    for opt in ENTRY_DURATION_OPTIONS:
        try:
            if abs(float(opt["value"]) - float(hours)) < 1e-9:
                return opt["value"]
        except (TypeError, ValueError):
            continue
    return DEFAULT_ENTRY_DURATION


def _entry_block_ids(row_index: int) -> tuple[str, str, str, str]:
    """Return (project_block_id, task_block_id, category_block_id, accomplishment_block_id)."""
    return (
        f"entry_{row_index}_project_block",
        f"entry_{row_index}_task_block",
        f"entry_{row_index}_category_block",
        f"entry_{row_index}_accomplishment_block",
    )


def _entry_is_filled(state: dict | None, row_index: int) -> bool:
    """True when a work entry (or Break) is complete enough to free another blank slot."""
    (
        project_block_id,
        task_block_id,
        category_block_id,
        accomplishment_block_id,
    ) = _entry_block_ids(row_index)
    project_key = _state_value(state, project_block_id, "project_select", "selected_option")
    if not project_key:
        return False
    if project_key == BREAK_PROJECT_VALUE:
        return True
    task_key = _state_value(state, task_block_id, "task_select", "selected_option")
    category_key = _state_value(state, category_block_id, "category_select", "selected_option")
    accomplishment = _state_value(state, accomplishment_block_id, "accomplishment_input")
    if task_key in PLACEHOLDER_TASK_VALUES:
        task_key = None
    return bool(task_key and category_key and accomplishment and str(accomplishment).strip())


def _row_count_from_view_state(state: dict | None) -> int:
    """How many entry slots are present in the current modal state."""
    if not state:
        return MIN_ENTRY_ROWS
    count = 0
    while (
        _duration_block_id(count) in state
        or f"entry_{count}_project_block" in state
        or f"entry_{count}_accomplishment_block" in state
    ):
        count += 1
        if count >= MAX_ENTRY_ROWS:
            break
    return max(count, MIN_ENTRY_ROWS)


def _row_count_for_state(state: dict | None, current_row_count: int | None = None) -> int:
    """Always leave one blank slot after filled rows (min 2, max MAX_ENTRY_ROWS)."""
    base = current_row_count or _row_count_from_view_state(state)
    filled = sum(1 for i in range(base) if _entry_is_filled(state, i))
    return min(MAX_ENTRY_ROWS, max(MIN_ENTRY_ROWS, filled + 1))


def _sanitize_task_selections(state: dict | None) -> dict | None:
    """Drop task selections that don't belong to the row's current project."""
    if not state:
        return state
    for row_index in range(MAX_ENTRY_ROWS):
        project_block_id, task_block_id, _, _ = _entry_block_ids(row_index)
        if project_block_id not in state and task_block_id not in state:
            continue
        project_key = _state_value(state, project_block_id, "project_select", "selected_option")
        task_key = _state_value(state, task_block_id, "task_select", "selected_option")
        if not task_key:
            continue
        valid_ids = {opt["value"] for opt in _task_options(project_key)}
        if task_key not in valid_ids or task_key in PLACEHOLDER_TASK_VALUES:
            block = dict(state.get(task_block_id) or {})
            block.pop("task_select", None)
            if block:
                state[task_block_id] = block
            else:
                state.pop(task_block_id, None)
    return state


def _entry_row_blocks(
    row_index: int,
    preserve_state: dict | None = None,
) -> list[dict]:
    (
        project_block_id,
        task_block_id,
        category_block_id,
        accomplishment_block_id,
    ) = _entry_block_ids(row_index)
    duration_block_id = _duration_block_id(row_index)
    project_options = _project_options(allow_break=True)
    saved_project = _state_value(
        preserve_state, project_block_id, "project_select", "selected_option"
    )
    task_options = _task_options(saved_project)
    category_options = _category_options()

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
        "dispatch_action_config": {
            "trigger_actions_on": ["on_character_entered"],
        },
        "placeholder": {"type": "plain_text", "text": "What did you accomplish?"},
    }

    saved_task = _state_value(
        preserve_state, task_block_id, "task_select", "selected_option"
    )
    saved_category = _state_value(
        preserve_state, category_block_id, "category_select", "selected_option"
    )
    saved_accomplishment = _state_value(
        preserve_state, accomplishment_block_id, "accomplishment_input"
    )

    if saved_project:
        matched = next((opt for opt in project_options if opt["value"] == saved_project), None)
        if matched:
            project_element["initial_option"] = matched
    if saved_task and saved_task not in PLACEHOLDER_TASK_VALUES:
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
                    "Create a new category",
                ),
            ],
        },
        {
            "type": "input",
            "block_id": accomplishment_block_id,
            "dispatch_action": True,
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
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Log your activities. Each entry has its own duration "
                    "(defaults to *1.0 hr*). Leave unused entries blank — "
                    "they count as Break and are not written to the journal."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Use *+* next to Project / Task / Category to create a new one. "
                        "Tasks belong to the selected project. "
                        "A new blank entry appears when every visible entry is filled."
                    ),
                }
            ],
        },
    ]

    for row_index in range(row_count):
        blocks.extend(_entry_row_blocks(row_index, preserve_state))

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
        (
            project_block_id,
            task_block_id,
            category_block_id,
            accomplishment_block_id,
        ) = _entry_block_ids(row_index)
        duration_block_id = _duration_block_id(row_index)

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

        project_key = _state_value(
            state_values, project_block_id, "project_select", "selected_option"
        )
        task_key = _state_value(state_values, task_block_id, "task_select", "selected_option")
        category_key = _state_value(
            state_values, category_block_id, "category_select", "selected_option"
        )
        accomplishment = _state_value(state_values, accomplishment_block_id, "accomplishment_input")

        # Completely empty → treat as Break (not recorded; no break tally).
        if not project_key:
            continue

        if project_key == BREAK_PROJECT_VALUE:
            break_hours += hours
            continue

        if task_key in PLACEHOLDER_TASK_VALUES:
            task_key = None

        missing_fields = []
        if not task_key:
            missing_fields.append("task")
        if not category_key:
            missing_fields.append("category")
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
                category=category_key,
                activity=accomplishment.strip(),
                project_key=project_key,
            )
        )

    if not entries and not errors:
        errors[_entry_block_ids(0)[3]] = "Fill out at least one work entry before submitting."

    return entries, errors, break_hours


def _maybe_expand_logtime_view(ack, body, client) -> None:
    """Ack and rebuild the modal with an extra blank row when all slots are filled."""
    _rebuild_logtime_view(ack, body, client, force=False)


def _rebuild_logtime_view(ack, body, client, *, force: bool) -> None:
    """Rebuild logtime modal from current state (always when force=True)."""
    ack()
    view = body.get("view") or {}
    state = _sanitize_task_selections(dict(view.get("state", {}).get("values", {}) or {}))
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    current = int(meta.get("row_count") or _row_count_from_view_state(state))
    desired = _row_count_for_state(state, current)
    if not force and desired <= current:
        return
    meta["row_count"] = max(desired, current) if force else desired
    row_count = meta["row_count"]
    updated = build_logtime_modal(row_count=row_count, preserve_state=state)
    updated["private_metadata"] = json.dumps(meta)
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
    # Do not store preserve_state — Slack private_metadata max is 3000 chars.
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
    return {
        "type": "modal",
        "callback_id": f"create_{kind}_modal",
        "title": {"type": "plain_text", "text": titles[kind]},
        "submit": {"type": "plain_text", "text": "Create"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(parent_meta),
        "blocks": blocks,
    }


def _refresh_parent_logtime(client, parent_meta: dict) -> None:
    view_id = parent_meta.get("parent_view_id")
    if not view_id:
        return
    row_count = int(parent_meta.get("row_count") or MIN_ENTRY_ROWS)
    updated = build_logtime_modal(row_count=row_count)
    root_meta = {
        "channel_id": parent_meta.get("channel_id", ""),
        "user_id": parent_meta.get("user_id", ""),
        "row_count": row_count,
    }
    updated["private_metadata"] = json.dumps(root_meta)
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
    _maybe_expand_logtime_view(ack, body, client)


@app.action("category_select")
def handle_category_select(ack, body, client):
    _maybe_expand_logtime_view(ack, body, client)


@app.action("entry_duration_select")
def handle_entry_duration_select(ack, body, client):
    _maybe_expand_logtime_view(ack, body, client)


@app.action("accomplishment_input")
def handle_accomplishment_input(ack, body, client):
    _maybe_expand_logtime_view(ack, body, client)


def _push_create_modal(ack, body, client, kind: str):
    ack()
    trigger_id = body.get("trigger_id")
    if not trigger_id:
        return
    parent_meta = _parent_metadata_from_view(body.get("view") or {})
    root_meta = {}
    try:
        root_meta = json.loads((body.get("view") or {}).get("private_metadata") or "{}")
    except Exception:
        root_meta = {}
    parent_meta["channel_id"] = root_meta.get("channel_id", "")
    parent_meta["user_id"] = root_meta.get("user_id") or body.get("user", {}).get("id", "")
    client.views_push(
        trigger_id=trigger_id,
        view=_build_create_named_modal(kind, parent_meta),
    )


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
    state = view.get("state", {}).get("values", {})
    project_block_id, _, _, _ = _entry_block_ids(row_index)
    project_id = _state_value(state, project_block_id, "project_select", "selected_option")
    user_id = body.get("user", {}).get("id", "")
    channel_id = ""
    try:
        root_meta = json.loads(view.get("private_metadata") or "{}")
        channel_id = root_meta.get("channel_id") or ""
        user_id = root_meta.get("user_id") or user_id
    except Exception:
        root_meta = {}

    if not project_id or project_id == BREAK_PROJECT_VALUE:
        client.chat_postEphemeral(
            channel=channel_id or user_id,
            user=user_id,
            text="Select a project on that entry first, then use *+* to add a task for it.",
        )
        return

    parent_meta = _parent_metadata_from_view(view)
    parent_meta["channel_id"] = channel_id
    parent_meta["user_id"] = user_id
    parent_meta["project_id"] = project_id
    parent_meta["row_index"] = row_index
    try:
        project = firestore_store.get_project(project_id)
        parent_meta["project_name"] = project.name if project else project_id
    except Exception:
        parent_meta["project_name"] = project_id
    client.views_push(
        trigger_id=trigger_id,
        view=_build_create_named_modal("task", parent_meta),
    )


@app.action("create_category_btn")
def handle_create_category_btn(ack, body, client):
    _push_create_modal(ack, body, client, "category")


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
    _refresh_parent_logtime(client, parent_meta)
    user_id = body.get("user", {}).get("id") or parent_meta.get("user_id")
    project_label = parent_meta.get("project_name") or project_id
    if user_id:
        client.chat_postEphemeral(
            channel=parent_meta.get("channel_id") or user_id,
            user=user_id,
            text=(
                f"✅ Created task *{created.name}* for *{project_label}*. "
                "It is now in that project's Task dropdown."
            ),
        )


@app.view("create_category_modal")
def handle_create_category_modal(ack, body, client, view):
    name = (_state_value(view.get("state", {}).get("values", {}), "name_block", "name_input") or "").strip()
    if not name:
        ack(response_action="errors", errors={"name_block": "Enter a category name."})
        return
    try:
        created = firestore_store.create_category(name)
    except Exception as exc:
        ack(response_action="errors", errors={"name_block": f"Could not create category: {exc}"})
        return
    ack()
    parent_meta = json.loads(view.get("private_metadata") or "{}")
    _refresh_parent_logtime(client, parent_meta)
    user_id = body.get("user", {}).get("id") or parent_meta.get("user_id")
    if user_id:
        client.chat_postEphemeral(
            channel=parent_meta.get("channel_id") or user_id,
            user=user_id,
            text=(
                f"✅ Created category *{created.name}* (`{created.id}`). "
                "It is now in the Category dropdown."
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
    _refresh_parent_logtime(client, parent_meta)


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
    user_name = user.get("name") or user.get("username", "Unknown User")
    state_values = view.get("state", {}).get("values", {})

    entries, errors, break_hours = _parse_modal_submission(state_values, user_name)
    if errors:
        ack(response_action="errors", errors=errors)
        return

    ack()

    duration = (
        _hours_to_option_value(entries[0].hours) if entries else DEFAULT_ENTRY_DURATION
    )

    entries_by_project: dict[str, list[jm.LogEntry]] = defaultdict(list)
    for entry in entries:
        entries_by_project[entry.project_key].append(entry)

    metadata = json.loads(view.get("private_metadata") or "{}")
    channel_id = metadata.get("channel_id")
    user_id = user.get("id") or metadata.get("user_id")

    if entries and user_id:
        try:
            firestore_store.set_last_logtime(
                user_id,
                duration,
                [
                    firestore_store.LastLogEntry(
                        project_key=entry.project_key,
                        task=entry.task,
                        category=entry.category,
                        activity=entry.activity,
                        hours=_hours_to_option_value(entry.hours),
                    )
                    for entry in entries
                ],
            )
        except Exception as exc:
            print(f"[SLACK WARNING] Could not save last logtime for {user_id}: {exc}", flush=True)

    total_hours = round(sum(entry.hours for entry in entries), 2)
    print(
        f"[SLACK SOCKET] Modal submitted by {user_name}: "
        f"{len(entries)} journal entries ({total_hours:g} hr), "
        f"break={break_hours:g} hr"
    )

    results = []
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
        )
        project_hours = round(sum(entry.hours for entry in project_entries), 2)
        if result.log_appended and result.summary_updated:
            refresh_note = " Doc tables synced." if result.docs_refreshed else ""
            results.append(
                f"✅ *{project_name}* (Doc `{assets.document_title}`): "
                f"logged {project_hours:g} hr(s) to Sheet "
                f"and refreshed weekly narrative.{refresh_note}"
            )
        elif result.log_appended:
            results.append(
                f"⚠️ *{project_name}*: logged {project_hours:g} hr(s) to Sheet, "
                "but Doc narrative refresh failed."
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
