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
import hourly_reminder
import journal_models as jm
import project_router

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

PROJECT_OPTIONS = [
    {"text": {"type": "plain_text", "text": "Tahoe Backyard"}, "value": "tahoe_backyard"},
    {"text": {"type": "plain_text", "text": "Wood Energy Facility"}, "value": "wood_energy_facility"},
    {"text": {"type": "plain_text", "text": "8494 Speckled Ave"}, "value": "8494_speckled"},
]

TASK_OPTIONS = [
    {"text": {"type": "plain_text", "text": "CAD / BIM Modeling"}, "value": "cad_modeling"},
    {"text": {"type": "plain_text", "text": "Permitting / Code Review"}, "value": "permitting"},
    {"text": {"type": "plain_text", "text": "Engineering / Calcs"}, "value": "engineering"},
]

DURATION_OPTIONS = [
    {"text": {"type": "plain_text", "text": "1.0 hr (single activity)"}, "value": "1.0"},
    {"text": {"type": "plain_text", "text": "0.5 hr (two activities)"}, "value": "0.5"},
    {"text": {"type": "plain_text", "text": "0.25 hr (four activities)"}, "value": "0.25"},
]


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


def _entry_row_blocks(row_index: int, duration: str, preserve_state: dict | None = None) -> list[dict]:
    project_block_id = f"entry_{row_index}_project_block"
    task_block_id = f"entry_{row_index}_task_block"
    accomplishment_block_id = f"entry_{row_index}_accomplishment_block"

    project_element = {
        "type": "static_select",
        "action_id": "project_select",
        "placeholder": {"type": "plain_text", "text": "Select a Project"},
        "options": PROJECT_OPTIONS,
    }
    task_element = {
        "type": "static_select",
        "action_id": "task_select",
        "placeholder": {"type": "plain_text", "text": "Select Task Type"},
        "options": TASK_OPTIONS,
    }
    accomplishment_element = {
        "type": "plain_text_input",
        "action_id": "accomplishment_input",
        "multiline": True,
        "placeholder": {"type": "plain_text", "text": "What did you accomplish?"},
    }

    saved_project = _state_value(preserve_state, project_block_id, "project_select", "selected_option")
    saved_task = _state_value(preserve_state, task_block_id, "task_select", "selected_option")
    saved_accomplishment = _state_value(preserve_state, accomplishment_block_id, "accomplishment_input")

    if saved_project:
        project_element["initial_option"] = next(
            opt for opt in PROJECT_OPTIONS if opt["value"] == saved_project
        )
    if saved_task:
        task_element["initial_option"] = next(
            opt for opt in TASK_OPTIONS if opt["value"] == saved_task
        )
    if saved_accomplishment:
        accomplishment_element["initial_value"] = saved_accomplishment

    row_label = f"Entry {row_index + 1} ({duration} hr)"
    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{row_label}*"},
        },
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
            "label": {"type": "plain_text", "text": "Task Category"},
        },
        {
            "type": "input",
            "block_id": accomplishment_block_id,
            "element": accomplishment_element,
            "label": {"type": "plain_text", "text": "What did you accomplish?"},
        },
    ]


def build_logtime_modal(duration: str = "1.0", preserve_state: dict | None = None) -> dict:
    row_count = jm.DURATION_ROW_COUNTS.get(duration, 1)
    duration_option = next(opt for opt in DURATION_OPTIONS if opt["value"] == duration)

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

    for row_index in range(row_count):
        blocks.extend(_entry_row_blocks(row_index, duration, preserve_state))

    return {
        "type": "modal",
        "callback_id": "logtime_modal",
        "title": {"type": "plain_text", "text": "Hourly Project Update"},
        "submit": {"type": "plain_text", "text": "Submit Time & Journal"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _parse_modal_submission(state_values: dict, user_name: str) -> tuple[list[jm.LogEntry], dict]:
    errors = {}
    duration = _state_value(state_values, "duration_block", "duration_select", "selected_option") or "1.0"
    row_count = jm.DURATION_ROW_COUNTS.get(duration, 1)
    hours = float(duration)
    now = datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE))
    entries = []

    for row_index in range(row_count):
        project_block_id = f"entry_{row_index}_project_block"
        task_block_id = f"entry_{row_index}_task_block"
        accomplishment_block_id = f"entry_{row_index}_accomplishment_block"

        project_key = _state_value(state_values, project_block_id, "project_select", "selected_option")
        task_category = _state_value(state_values, task_block_id, "task_select", "selected_option")
        accomplishment = _state_value(state_values, accomplishment_block_id, "accomplishment_input")

        if not project_key:
            errors[project_block_id] = "Select a project."
        if not task_category:
            errors[task_block_id] = "Select a task category."
        if not accomplishment or not accomplishment.strip():
            errors[accomplishment_block_id] = "Describe what you accomplished."

        if project_key and task_category and accomplishment and accomplishment.strip():
            entries.append(
                jm.LogEntry(
                    timestamp=now,
                    user=user_name,
                    hours=hours,
                    category=task_category,
                    activity=accomplishment.strip(),
                    project_key=project_key,
                )
            )

    return entries, errors


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

    doc_id = project_router.get_journal_id_for_project(project_key)
    project_name = project_router.get_project_display_name(project_key)
    if not doc_id:
        client.chat_postEphemeral(
            channel=body["channel_id"],
            user=user_id,
            text=f"No Google Doc mapped for '{project_key}'.",
        )
        return

    result = agent_journal.refresh_weekly_summary(doc_id, project_name)
    if result.summary_updated:
        text = f"✅ Refreshed weekly summary for *{project_name}*."
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


@app.view("logtime_modal")
def handle_logtime_submission(ack, body, client, view):
    user = body.get("user", {})
    user_name = user.get("name") or user.get("username", "Unknown User")
    state_values = view.get("state", {}).get("values", {})

    entries, errors = _parse_modal_submission(state_values, user_name)
    if errors:
        ack(response_action="errors", errors=errors)
        return

    ack()

    entries_by_project: dict[str, list[jm.LogEntry]] = defaultdict(list)
    for entry in entries:
        entries_by_project[entry.project_key].append(entry)

    total_hours = round(sum(entry.hours for entry in entries), 2)
    print(f"[SLACK SOCKET] Modal submitted by {user_name}: {len(entries)} entries, {total_hours} hr")

    results = []
    for project_key, project_entries in entries_by_project.items():
        doc_id = project_router.get_journal_id_for_project(project_key)
        project_name = project_router.get_project_display_name(project_key)
        if not doc_id:
            results.append(
                f"⚠️ No Google Doc mapped for '{project_router.get_project_display_name(project_key)}'."
            )
            continue

        result = agent_journal.process_journal_update(doc_id, project_name, project_entries)
        project_hours = round(sum(entry.hours for entry in project_entries), 2)
        if result.log_appended and result.summary_updated:
            results.append(
                f"✅ *{project_name}*: logged {project_hours:g} hr(s) and refreshed weekly summary."
            )
        elif result.log_appended:
            results.append(
                f"⚠️ *{project_name}*: logged {project_hours:g} hr(s), but weekly summary refresh failed."
            )
        else:
            results.append(
                f"❌ *{project_name}*: could not write to Google Doc ({result.error_message or 'unknown error'})."
            )

    metadata = json.loads(view.get("private_metadata") or "{}")
    channel_id = metadata.get("channel_id")
    user_id = user.get("id") or metadata.get("user_id")
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
