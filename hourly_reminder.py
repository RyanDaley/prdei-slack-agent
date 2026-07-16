"""
Hourly Slack DM with Block Kit UI; button opens the logtime modal.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import journal_models as jm


def build_hourly_reminder_blocks() -> list[dict]:
    """Block Kit message shown each hour (matches the logtime form branding)."""
    now = datetime.now(ZoneInfo(jm.JOURNAL_TIMEZONE))
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Hourly Project Update",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*It's {now.strftime('%I:%M %p')}* — time to log the past hour.\n"
                    "Click *Open Time Entry Form* below to fill out project, task, "
                    "and accomplishments."
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Choose hours per entry (defaults to 1.0 hr). "
                        "Empty slots are treated as Break and not written to the journal."
                    ),
                }
            ],
        },
        {
            "type": "actions",
            "block_id": "hourly_reminder_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Time Entry Form"},
                    "style": "primary",
                    "action_id": "open_logtime_modal",
                }
            ],
        },
    ]


def _reminder_timezone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("REMINDER_TIMEZONE", jm.JOURNAL_TIMEZONE))


def resolve_reminder_user_id(client) -> str:
    user_id = os.environ.get("REMINDER_USER_ID", "").strip()
    if user_id:
        return user_id

    email = os.environ.get("REMINDER_USER_EMAIL", "").strip()
    if not email:
        return ""

    try:
        response = client.users_lookupByEmail(email=email)
        return response["user"]["id"]
    except Exception as exc:
        print(f"[REMINDER ERROR] Could not look up Slack user by email '{email}': {exc}")
        return ""


def _is_reminder_enabled(user_id: str) -> bool:
    if not user_id:
        return False
    enabled = os.environ.get("REMINDER_ENABLED", "true").strip().lower()
    return enabled in {"1", "true", "yes", "on"}


def _is_work_time(now: datetime) -> bool:
    work_days = os.environ.get("REMINDER_WORK_DAYS", "mon,tue,wed,thu,fri").lower()
    allowed_days = {day.strip()[:3] for day in work_days.split(",") if day.strip()}
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    if day_names[now.weekday()] not in allowed_days:
        return False

    start_hour = int(os.environ.get("REMINDER_WORK_START", "8"))
    end_hour = int(os.environ.get("REMINDER_WORK_END", "18"))
    # Inclusive end hour so REMINDER_WORK_END=18 still sends at 18:00.
    return start_hour <= now.hour <= end_hour


def _seconds_until_next_hour(tz: ZoneInfo) -> float:
    now = datetime.now(tz)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(1.0, (next_hour - now).total_seconds())


def send_hourly_reminder(client, user_id: str) -> bool:
    try:
        dm = client.conversations_open(users=user_id)
        channel_id = dm["channel"]["id"]
        client.chat_postMessage(
            channel=channel_id,
            text="Hourly Project Update — open the time entry form to log your hour.",
            blocks=build_hourly_reminder_blocks(),
        )
        print(f"[REMINDER] Posted hourly Block Kit message to {user_id}")
        return True
    except Exception as exc:
        print(f"[REMINDER ERROR] Failed to send hourly blocks to {user_id}: {exc}")
        return False


def _reminder_loop(client, user_id: str) -> None:
    tz = _reminder_timezone()
    print(
        f"[REMINDER] Hourly Block Kit scheduler active for {user_id} "
        f"(timezone={tz.key})"
    )

    if os.environ.get("REMINDER_SEND_ON_START", "").strip().lower() in {"1", "true", "yes"}:
        if _is_work_time(datetime.now(tz)):
            send_hourly_reminder(client, user_id)

    while True:
        time.sleep(_seconds_until_next_hour(tz))
        now = datetime.now(tz)
        if not _is_work_time(now):
            print(f"[REMINDER] Skipping {now.strftime('%A %H:%M')} (outside work hours).")
            continue
        send_hourly_reminder(client, user_id)


def start_hourly_reminder_thread(client) -> None:
    user_id = resolve_reminder_user_id(client)
    if not _is_reminder_enabled(user_id):
        print(
            "[REMINDER] Hourly blocks disabled. Set REMINDER_USER_ID or "
            "REMINDER_USER_EMAIL in env.yaml."
        )
        return

    thread = threading.Thread(
        target=_reminder_loop,
        args=(client, user_id),
        name="hourly-reminder",
        daemon=True,
    )
    thread.start()
