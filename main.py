import json
import os
import threading
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import agent_journal
import agent_timesheets
import hourly_reminder
import journal_models as jm
import project_router

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

PROJECT_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Tahoe Backyard"}, "value": "tahoe_backyard"},
    {"text": {"type": "plain_text", "text": "Wood Energy Facility"}, "value": "wood_energy_facility"},
    {"text": {"type": "plain_text", "text": "8494 Speckled Ave"}, "value": "8494_speckled"},
]

BREAK_PROJECT_VALUE = "break"
BREAK_PROJECT_OPTION = {
    "text": {"type": "plain_text", "text": "Break"},
    "value": BREAK_PROJECT_VALUE,
}

TASK_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Project Management"}, "value": "project_management"},
    {"text": {"type": "plain_text", "text": "Schematic Design"}, "value": "schematic_design"},
    {"text": {"type": "plain_text", "text": "Design Development"}, "value": "design_development"},
    {"text": {"type": "plain_text", "text": "Construction Documents"}, "value": "construction_documents"},
]

CATEGORY_OPTIONS = [
    {"text": {"type": "plain_text", "text": "CAD / BIM Modeling"}, "value": "cad_modeling"},
    {"text": {"type": "plain_text", "text": "Permitting / Code Review"}, "value": "permitting"},
    {"text": {"type": "plain_text", "text": "Engineering / Calcs"}, "value": "engineering"},
]

DURATION_OPTIONS = [
    {"text": {"type": "plain_text", "text": "1.0 hr (single activity)"}, "value": "1.0"},
    {"text": {"type": "plain_text", "text": "0.5 hr (two activities)"}, "value": "0.5"},
    {"text": {"type": "plain_text", "text": "0.25 hr (four activities)"}, "value": "0.25"},
]


def _project_options(*, allow_break: bool) -> list[dict]:
    if allow_break:
        return [*PROJECT_OPTIONS, BREAK_PROJECT_OPTION]
    return PROJECT_OPTIONS


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


def _entry_block_ids(row_index: int, side_by_side: bool) -> tuple[str, str, str, str]:
    """Return (project_block_id, task_block_id, category_block_id, accomplishment_block_id)."""
    accomplishment_block_id = f"entry_{row_index}_accomplishment_block"
    if side_by_side:
        # Project + Task + Category share one actions block (horizontal row).
        shared = f"entry_{row_index}_fields_block"
        return shared, shared, shared, accomplishment_block_id
    return (
        f"entry_{row_index}_project_block",
        f"entry_{row_index}_task_block",
        f"entry_{row_index}_category_block",
        accomplishment_block_id,
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
    ) = _entry_block_ids(row_index, side_by_side)
    project_options = _project_options(allow_break=side_by_side)

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
        "options": TASK_OPTIONS,
    }
    category_element = {
        "type": "static_select",
        "action_id": "category_select",
        "placeholder": {"type": "plain_text", "text": "Select a Category"},
        "options": CATEGORY_OPTIONS,
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
        matched_task = next((opt for opt in TASK_OPTIONS if opt["value"] == saved_task), None)
        if matched_task:
            task_element["initial_option"] = matched_task
    if saved_category:
        matched_cat = next(
            (opt for opt in CATEGORY_OPTIONS if opt["value"] == saved_category), None
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
    ]

    if side_by_side:
        # Slack renders multiple elements in an actions block on one row.
        blocks.append(
            {
                "type": "actions",
                "block_id": project_block_id,
                "elements": [project_element, task_element, category_element],
            }
        )
    else:
        blocks.extend(
            [
                {
                    "type": "input",
                    "block_id": project_block_id,
                    "element": project_element,
                    "label": {"type": "plain_text", "text": "Project Name"},
                },
                {
                    "type": "input",
                    "block_id": task_block_id,
                    "element": task_element,
                    "label": {"type": "plain_text", "text": "Task"},
                },
                {
                    "type": "input",
                    "block_id": category_block_id,
                    "element": category_element,
                    "label": {"type": "plain_text", "text": "Category"},
                },
            ]
        )

    blocks.append(
        {
            "type": "input",
            "block_id": accomplishment_block_id,
            "element": accomplishment_element,
            "label": {"type": "plain_text", "text": "What did you accomplish?"},
            "optional": side_by_side,
        }
    )
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
        ) = _entry_block_ids(row_index, side_by_side)

        project_key = _state_value(state_values, project_block_id, "project_select", "selected_option")
        task_key = _state_value(state_values, task_block_id, "task_select", "selected_option")
        category_key = _state_value(
            state_values, category_block_id, "category_select", "selected_option"
        )
        accomplishment = _state_value(state_values, accomplishment_block_id, "accomplishment_input")

        if not project_key:
            if side_by_side:
                errors[accomplishment_block_id] = "Select a project (or Break)."
            else:
                errors[project_block_id] = "Select a project."
            continue

        if project_key == BREAK_PROJECT_VALUE:
            if not side_by_side:
                errors[project_block_id] = "Break is only available for split-hour entries."
                continue
            break_hours += hours
            continue

        if side_by_side:
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
        else:
            if not task_key:
                errors[task_block_id] = "Select a task."
            if not category_key:
                errors[category_block_id] = "Select a category."
            if not accomplishment or not accomplishment.strip():
                errors[accomplishment_block_id] = "Describe what you accomplished."
            if (
                task_block_id in errors
                or category_block_id in errors
                or accomplishment_block_id in errors
            ):
                continue

        if (
            not task_key
            or not category_key
            or not accomplishment
            or not accomplishment.strip()
        ):
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
                "Set PROJECT_DOC_MAP to that project's Google Drive folder URL."
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
    """No-op: actions-block selects must be acked to avoid Unhandled request."""
    ack()


@app.action("task_select")
def handle_task_select(ack):
    """No-op: actions-block selects must be acked to avoid Unhandled request."""
    ack()


@app.action("category_select")
def handle_category_select(ack):
    """No-op: actions-block selects must be acked to avoid Unhandled request."""
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


def _slack_employee_identity(client, user_id: str) -> tuple[str, str]:
    """
    Return (display_name, last_name) from Slack profile for timesheet headers/titles.
    """
    display = "Employee"
    last_name = "Employee"
    try:
        info = client.users_info(user=user_id)
        user_obj = (info or {}).get("user") or {}
        profile = user_obj.get("profile") or {}
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
                f"Set `PROJECT_FOLDER_{project_key.upper()}` in env.yaml or "
                "PROJECT_DOC_MAP, then redeploy."
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
        display_name, last_name = _slack_employee_identity(client, user_id)
        ts_result = agent_timesheets.process_timesheet_update(
            slack_user_id=user_id,
            employee_display_name=display_name,
            last_name=last_name,
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


class DummyHealthCheckServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), DummyHealthCheckServer)
    print(f"Dummy health check server listening on port {port}...")
    server.serve_forever()


if __name__ == "__main__":
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
