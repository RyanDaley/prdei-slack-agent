import os
import json
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import project_router
import agent_journal

# 1. Initialize your Slack App using your standard Bot Token (xoxb-)
# Bolt automatically reads the token from the environment variable SLACK_BOT_TOKEN
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# --- SLACK INTERACTIVE UI BLOCK KIT ---
# This dictionary matches the exact UI layout you built in the Block Kit Builder.
TIMESHEET_BLOCKS = [
    {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": "⏱️ Hourly Project Update",
            "emoji": True
        }
    },
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "Please log your activities for the past hour to update the project journal and timesheet."
        }
    },
    {
        "type": "divider"
    },
    {
        "type": "input",
        "block_id": "project_block",
        "element": {
            "type": "static_select",
            "placeholder": {
                "type": "plain_text",
                "text": "Select a Project"
            },
            "options": [
                {"text": {"type": "plain_text", "text": "Tahoe Backyard"}, "value": "tahoe_backyard"},
                {"text": {"type": "plain_text", "text": "Wood Energy Facility"}, "value": "wood_energy_facility"},
                {"text": {"type": "plain_text", "text": "8494 Speckled Ave"}, "value": "8494_speckled"}
            ],
            "action_id": "project_select"
        },
        "label": {"type": "plain_text", "text": "Project Name"}
    },
    {
        "type": "input",
        "block_id": "task_block",
        "element": {
            "type": "static_select",
            "placeholder": {
                "type": "plain_text",
                "text": "Select Task Type"
            },
            "options": [
                {"text": {"type": "plain_text", "text": "CAD / BIM Modeling"}, "value": "cad_modeling"},
                {"text": {"type": "plain_text", "text": "Permitting / Code Review"}, "value": "permitting"},
                {"text": {"type": "plain_text", "text": "Engineering / Calcs"}, "value": "engineering"}
            ],
            "action_id": "task_select"
        },
        "label": {"type": "plain_text", "text": "Task Category"}
    },
    {
        "type": "input",
        "block_id": "accomplishment_block",
        "element": {
            "type": "plain_text_input",
            "multiline": True,
            "action_id": "accomplishment_input",
            "placeholder": {"type": "plain_text", "text": "What did you accomplish?"}
        },
        "label": {"type": "plain_text", "text": "What did you accomplish?"}
    },
    {
        "type": "actions",
        "block_id": "submit_block",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Submit Time & Journal"},
                "style": "primary",
                "value": "submit_timesheet",
                "action_id": "submit_button"
            }
        ]
    }
]


# 2. Handle /logtime slash command over the Socket Mode pipeline
@app.command("/logtime")
def handle_logtime_command(ack, body, respond):
    """
    Triggers when a user types /logtime.
    Responds ephemerally (only visible to that user) with the Block Kit UI.
    """
    ack()
    print(f"[SLACK SOCKET] Received /logtime command from {body.get('user_name')}")
    
    # Respond sends the Block Kit payload instantly back to the chat thread
    respond(
        response_type="ephemeral",
        blocks=TIMESHEET_BLOCKS
    )


# 3. Intercept and process form clicks on the Submit Button
@app.action("submit_button")
def handle_timesheet_submission(ack, body, respond):
    """
    Executes when the user clicks 'Submit Time & Journal'.
    Extracts inputs from the message's current state blocks, maps the target Doc,
    calls Gemini to format, and appends directly to Google Docs.
    """
    ack()
    
    try:
        user_name = body.get("user", {}).get("name") or body.get("user", {}).get("username", "Unknown User")
        state_values = body.get("state", {}).get("values", {})
        
        # Navigate the state tree matching your Block Kit IDs exactly
        project_key = state_values.get("project_block", {}).get("project_select", {}).get("selected_option", {}).get("value", "N/A")
        task_category = state_values.get("task_block", {}).get("task_select", {}).get("selected_option", {}).get("value", "N/A")
        accomplishment = state_values.get("accomplishment_block", {}).get("accomplishment_input", {}).get("value", "N/A")
        
        print(f"[SLACK SOCKET] Clicked Submit. Project: {project_key} | User: {user_name}")

        # Send an immediate processing note to give visual feedback to the user
        respond("Processing your entry and running Gemini technical formatting...", replace_original=True)

        # Look up your routing map to find the correct Google Doc
        doc_id = project_router.get_journal_id_for_project(project_key)
        
        if doc_id:
            # Dispatch to formatting and appending pipeline
            success = agent_journal.append_to_journal(
                document_id=doc_id,
                user_name=user_name,
                task_category=task_category,
                raw_details=accomplishment
            )
            
            if success:
                # Replaces the interactive block with a clean confirmation message
                respond(f"✅ Successfully logged 1.0 hours and appended formatted entry to your Google Doc Journal!", replace_original=True)
            else:
                respond("❌ Error: Could not write to the Google Doc. Please verify permissions in Cloud Run logs.", replace_original=True)
        else:
            respond(f"⚠️ Mapped your time locally, but no active Google Doc journal URL is mapped for '{project_key}'.", replace_original=True)

    except Exception as exc:
        print(f"[ERROR] Fail in action handler: {exc}")
        respond(f"⚠️ Internal processing error while recording: {exc}", replace_original=True)

# Silence noisy dropdown state-change warnings
@app.action("project_select")
def handle_project_dropdown(ack):
    ack()

@app.action("task_select")
def handle_task_dropdown(ack):
    ack()

# 4. Start the outbound persistent WebSocket listener
if __name__ == "__main__":
    # Ensure SLACK_APP_TOKEN (starts with xapp-) and SLACK_BOT_TOKEN (starts with xoxb-) are loaded
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        print("[CRITICAL] SLACK_APP_TOKEN is missing. Outbound socket connection cannot start.")
    else:
        print("⚡ Bolt app is running in Socket Mode! Listening for Slack events...")
        handler = SocketModeHandler(app, app_token)
        handler.start()