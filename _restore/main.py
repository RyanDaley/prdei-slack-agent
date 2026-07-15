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

FALLBACK_TASK_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Project Management"}, "value": "project_management"},
    {"text": {"type": "plain_text", "text": "Schematic Design"}, "value": "schematic_design"},
    {"text": {"type": "plain_text", "text": "Design Development"}, "value": "design_development"},
    {"text": {"type": "plain_text", "text": "Construction Documents"}, "value": "construction_documents"},
]

FALLBACK_CATEGORY_OPTIONS = [
    {"text": {"type": "plain_text", "text": "CAD / BIM Modeling"}, "value": "cad_modeling"},
    {"text": {"type": "plain_text", "text": "Permitting / Code Review"}, "value": "permitting"},
    {"text": {"type": "plain_text", "text": "Engineering / Calcs"}, "value": "engineering"},
]

DURATION_OPTIONS = [
    {"text": {"type": "plain_text", "text": "1.0 hr (single activity)"}, "value": "1.0"},
    {"text": {"type": "plain_text", "text": "0.5 hr (two activities)"}, "value": "0.5"},
    {"text": {"type": "plain_text", "text": "0.25 hr (four activities)"}, "value": "0.25"},
]


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


def _task_options() -> list[dict]:
    try:
        tasks = firestore_store.list_tasks()
        if tasks:
            return [_option(t.name, t.id) for t in tasks]
    except Exception as exc:
        print(f"[SLACK] Firestore task options failed; using fallback: {exc}", flush=True)
    return list(FALLBACK_TASK_OPTIONS)


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
    modal = build_logtime_modal()
    modal["private_metadata"] = json.dumps(
        {"channel_id": channel_id, "user_id": user_id}
    )
    client.views_open(trigger_id=trigger_id, view=modal)


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


def _entry_block_ids(row_index: int) -> tuple[str, str, str, str]:
    """Return (project_block_id, task_block_id, category_block_id, accomplishment_block_id)."""
    return (
        f"entry_{row_index}_project_block",
        f"entry_{row_index}_task_block",
        f"entry_{row_index}_category_block",
        f"entry_{row_index}_accomplishment_block",
    )


def _entry_row_blocks(
    row_index: int,
    duration: str,
    preserve_state: dict | None = None,
    *,
    side_by_side: bool = False,
) -> list[dict]:
    (
        project_block_id,
        task_block_id,
        category_block_id,
        accomplishment_block_id,
    ) = _entry_block_ids(row_index)
    project_options = _project_options(allow_break=side_by_side)
    task_options = _task_options()
    category_options = _category_options()

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

    saved_project = _state_value(
        preserve_state, project_block_id, "project_select", "selected_option"
    )
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

    row_label = f"Entry {row_index + 1} ({duration} hr)"
    blocks: list[dict] = [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{row_label}*"},
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
                    "Create a new task",
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
            "element": accomplishment_element,
            "label": {"type": "plain_text", "text": "What did you accomplish?"},
            "optional": side_by_side,
        },
    ]
    return blocks


def build_logtime_modal(duration: str = "1.0", preserve_state: dict | None = None) -> dict:
    row_count = jm.DURATION_ROW_COUNTS.get(duration, 1)
    duration_option = next(opt for opt in DURATION_OPTIONS if opt["value"] == duration)
    side_by_side = row_count > 1

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Log your activities for the past hour. "
                    "Choose a duration to split the hour across multiple projects or tasks."
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
                        "(Slack shows the description via accessibility label, not a hover tip.)"
                    ),
                }
            ],
        },
        {
            "type": "input",
            "block_id": "duration_block",
            "dispatch_action": True,
            "element": {
                "type": "static_select",
                "action_id": "duration_select",
                "initial_option": duration_option,
                "options": DURATION_OPTIONS,
            },
            "label": {"type": "plain_text", "text": "Duration per entry"},
        },
    ]

    if side_by_side:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Choose *Break* on any unused slice — "
                            "Break entries are not written to the detailed activity log. "
                            "Task, Category, and accomplishment are optional for Break."
                        ),
                    }
                ],
            }
        )

    for row_index in range(row_count):
        blocks.extend(
            _entry_row_blocks(
                row_index,
                duration,
                preserve_state,
                side_by_side=side_by_side,
            )
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

    Returns (work_entries, errors, break_hours). Break rows are validated but not logged.
    """
    errors = {}
    duration = _state_value(state_values, "duration_block", "duration_select", "selected_option") or "1.0"
    row_count = jm.DURATION_ROW_COUNTS.get(duration, 1)
    side_by_side = row_count > 1
    hours = float(duration)
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

        project_key = _state_value(state_values, project_block_id, "project_select", "selected_option")
        task_key = _state_value(state_values, task_block_id, "task_select", "selected_option")
        category_key = _state_value(
            state_values, category_block_id, "category_select", "selected_option"
        )
        accomplishment = _state_value(state_values, accomplishment_block_id, "accomplishment_input")

        if not project_key:
            errors[accomplishment_block_id] = (
                "Select a project (or Break)." if side_by_side else "Select a project."
            )
            continue

        if project_key == BREAK_PROJECT_VALUE:
            if not side_by_side:
                errors[accomplishment_block_id] = "Break is only available for split-hour entries."
                continue
            break_hours += hours
            continue

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
                f"Complete {' / '.join(missing_fields)} for this entry."
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

    return entries, errors, break_hours


def _parent_metadata_from_view(view: dict) -> dict:
    meta = {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    meta["parent_view_id"] = view.get("id", "")
    meta["parent_view_hash"] = view.get("hash", "")
    duration = _state_value(
        view.get("state", {}).get("values", {}),
        "duration_block",
        "duration_select",
        "selected_option",
    ) or "1.0"
    meta["duration"] = duration
    meta["preserve_state"] = view.get("state", {}).get("values", {})
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
                            "the project's Google Drive folder."
                        ),
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
    duration = parent_meta.get("duration") or "1.0"
    preserve = parent_meta.get("preserve_state")
    updated = build_logtime_modal(duration=duration, preserve_state=preserve)
    # Keep original channel/user ids on the root modal.
    root_meta = {
        "channel_id": parent_meta.get("channel_id", ""),
        "user_id": parent_meta.get("user_id", ""),
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
def handle_project_select(ack):
    ack()


@app.action("task_select")
def handle_task_select(ack):
    ack()


@app.action("category_select")
def handle_category_select(ack):
    ack()


@app.action("duration_select")
def handle_duration_select(ack, body, client):
    ack()
    selected_duration = body["actions"][0]["selected_option"]["value"]
    updated_view = build_logtime_modal(
        duration=selected_duration,
        preserve_state=body["view"]["state"]["values"],
    )
    updated_view["private_metadata"] = body["view"].get("private_metadata", "")
    client.views_update(
        view_id=body["view"]["id"],
        hash=body["view"]["hash"],
        view=updated_view,
    )


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
    _push_create_modal(ack, body, client, "task")


@app.action("create_category_btn")
def handle_create_category_btn(ack, body, client):
    _push_create_modal(ack, body, client, "category")


@app.view("create_task_modal")
def handle_create_task_modal(ack, body, client, view):
    name = (_state_value(view.get("state", {}).get("values", {}), "name_block", "name_input") or "").strip()
    if not name:
        ack(response_action="errors", errors={"name_block": "Enter a task name."})
        return
    try:
        created = firestore_store.create_task(name)
    except Exception as exc:
        ack(response_action="errors", errors={"name_block": f"Could not create task: {exc}"})
        return
    ack()
    parent_meta = json.loads(view.get("private_metadata") or "{}")
    _refresh_parent_logtime(client, parent_meta)
    user_id = body.get("user", {}).get("id") or parent_meta.get("user_id")
    if user_id:
        client.chat_postEphemeral(
            channel=parent_meta.get("channel_id") or user_id,
            user=user_id,
            text=f"✅ Created task *{created.name}* (`{created.id}`). It is now in the Task dropdown.",
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

    ack(
        response_action="update",
        view={
            "type": "modal",
            "title": {"type": "plain_text", "text": "Project created"},
            "close": {"type": "plain_text", "text": "Done"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"✅ Created *{created.name}* (`{created.id}`).\n"
                            "Next: choose its Google Drive folder (link expires in 15 minutes)."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Choose Drive folder"},
                            "url": link if link.startswith("http") else "https://example.com",
                            "action_id": "open_drive_picker_link",
                        }
                    ],
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f"If the button doesn't open: `{link}`\n"
                                "Set `SERVICE_PUBLIC_URL` on Cloud Run if this is a relative path."
                            ),
                        }
                    ],
                },
            ],
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

    entries_by_project: dict[str, list[jm.LogEntry]] = defaultdict(list)
    for entry in entries:
        entries_by_project[entry.project_key].append(entry)

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

    metadata = json.loads(view.get("private_metadata") or "{}")
    channel_id = metadata.get("channel_id")
    user_id = user.get("id") or metadata.get("user_id")

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
    def do_GET(self):
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
