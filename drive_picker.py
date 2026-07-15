"""
Short-lived Drive folder picker pages for attaching a Project to a Drive folder.

Tokens are in-memory (single Cloud Run instance is fine for short sessions).
The page supports:
  1. Google Picker (when GOOGLE_PICKER_API_KEY + GOOGLE_OAUTH_CLIENT_ID are set)
  2. Paste-a-folder-URL fallback (always available)
"""

from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs

import firestore_store
import project_router

TOKEN_TTL_SECONDS = 15 * 60

_lock = threading.Lock()
_tokens: dict[str, "PickerSession"] = {}


@dataclass
class PickerSession:
    project_id: str
    project_name: str
    created_by: str
    created_at: float
    completed: bool = False
    folder_url: str = ""


def public_base_url() -> str:
    return (os.environ.get("SERVICE_PUBLIC_URL") or "").rstrip("/")


def create_picker_token(project_id: str, project_name: str, created_by: str) -> str:
    _purge_expired()
    token = secrets.token_urlsafe(24)
    with _lock:
        _tokens[token] = PickerSession(
            project_id=project_id,
            project_name=project_name,
            created_by=created_by,
            created_at=time.time(),
        )
    return token


def picker_url(token: str) -> str:
    base = public_base_url()
    if not base:
        return f"/drive-picker/{token}"
    return f"{base}/drive-picker/{token}"


def get_session(token: str) -> Optional[PickerSession]:
    _purge_expired()
    with _lock:
        session = _tokens.get(token)
        if not session:
            return None
        if time.time() - session.created_at > TOKEN_TTL_SECONDS:
            _tokens.pop(token, None)
            return None
        return session


def _purge_expired() -> None:
    now = time.time()
    with _lock:
        expired = [
            key
            for key, session in _tokens.items()
            if now - session.created_at > TOKEN_TTL_SECONDS
        ]
        for key in expired:
            _tokens.pop(key, None)


def complete_session(token: str, folder_url: str) -> tuple[bool, str]:
    """
    Persist the selected folder onto the project. Returns (ok, message).
    """
    session = get_session(token)
    if not session:
        return False, "This picker link has expired. Create the project again from Slack."

    folder_url = (folder_url or "").strip()
    folder_id = project_router.extract_id_from_url(folder_url, "folder")
    if not folder_id:
        return False, "Could not parse a Google Drive folder URL or ID."

    normalized = f"https://drive.google.com/drive/folders/{folder_id}"
    try:
        firestore_store.upsert_project(
            session.project_id,
            session.project_name,
            drive_folder_url=normalized,
        )
    except Exception as exc:
        return False, f"Failed to save folder on project: {exc}"

    with _lock:
        session.completed = True
        session.folder_url = normalized
    return True, "Folder saved."


def render_picker_page(token: str) -> tuple[int, str, str]:
    """Return (status_code, content_type, body)."""
    session = get_session(token)
    if not session:
        body = _page_shell(
            "Link expired",
            "<p>This Drive folder picker link has expired. Go back to Slack and create the project again.</p>",
        )
        return 410, "text/html; charset=utf-8", body

    if session.completed:
        body = _page_shell(
            "Done",
            (
                f"<p>Folder linked to <strong>{html.escape(session.project_name)}</strong>.</p>"
                "<p>You can close this tab and return to Slack.</p>"
            ),
        )
        return 200, "text/html; charset=utf-8", body

    api_key = os.environ.get("GOOGLE_PICKER_API_KEY", "").strip()
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    app_id = os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
    picker_enabled = bool(api_key and client_id)

    project_name = html.escape(session.project_name)
    safe_token = html.escape(token)

    picker_section = ""
    if picker_enabled:
        picker_section = f"""
        <div class="card">
          <h2>Browse Google Drive</h2>
          <p>Sign in with Google, then choose the Shared Drive folder for this project.</p>
          <button type="button" id="open-picker">Choose folder</button>
          <p id="picker-status" class="muted"></p>
        </div>
        <script src="https://accounts.google.com/gsi/client" async defer></script>
        <script src="https://apis.google.com/js/api.js"></script>
        <script>
          const API_KEY = {json.dumps(api_key)};
          const CLIENT_ID = {json.dumps(client_id)};
          const APP_ID = {json.dumps(app_id)};
          const TOKEN = {json.dumps(token)};
          let accessToken = '';

          function setStatus(msg) {{
            document.getElementById('picker-status').textContent = msg || '';
          }}

          function saveFolder(url) {{
            fetch('/drive-picker/' + TOKEN, {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{folder_url: url}})
            }}).then(r => r.json()).then(data => {{
              if (data.ok) {{
                window.location.reload();
              }} else {{
                setStatus(data.error || 'Save failed');
              }}
            }}).catch(err => setStatus(String(err)));
          }}

          function createPicker() {{
            const view = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
              .setIncludeFolders(true)
              .setSelectFolderEnabled(true)
              .setMimeTypes('application/vnd.google-apps.folder');
            const picker = new google.picker.PickerBuilder()
              .addView(view)
              .setOAuthToken(accessToken)
              .setDeveloperKey(API_KEY)
              .setCallback(function(data) {{
                if (data.action === google.picker.Action.PICKED && data.docs && data.docs[0]) {{
                  const id = data.docs[0].id;
                  saveFolder('https://drive.google.com/drive/folders/' + id);
                }}
              }});
            if (APP_ID) picker.setAppId(APP_ID);
            picker.build().setVisible(true);
          }}

          function loadPicker() {{
            gapi.load('picker', {{callback: createPicker}});
          }}

          document.getElementById('open-picker').addEventListener('click', function() {{
            setStatus('Opening Google sign-in…');
            const client = google.accounts.oauth2.initTokenClient({{
              client_id: CLIENT_ID,
              scope: 'https://www.googleapis.com/auth/drive.readonly',
              callback: (resp) => {{
                if (resp.error) {{
                  setStatus(resp.error);
                  return;
                }}
                accessToken = resp.access_token;
                setStatus('Choose a folder…');
                loadPicker();
              }}
            }});
            client.requestAccessToken();
          }});
        </script>
        """

    body = _page_shell(
        f"Link Drive folder — {project_name}",
        f"""
        <p>Project: <strong>{project_name}</strong></p>
        {picker_section}
        <div class="card">
          <h2>{"Or paste a folder URL" if picker_enabled else "Paste the Google Drive folder URL"}</h2>
          <form method="POST" action="/drive-picker/{safe_token}">
            <label for="folder_url">Folder URL</label>
            <input id="folder_url" name="folder_url" type="url" required
              placeholder="https://drive.google.com/drive/folders/..." />
            <button type="submit">Save folder</button>
          </form>
          <p class="muted">This link expires in 15 minutes.</p>
        </div>
        """,
    )
    return 200, "text/html; charset=utf-8", body


def handle_picker_post(token: str, raw_body: bytes, content_type: str) -> tuple[int, str, str]:
    folder_url = ""
    if "application/json" in (content_type or ""):
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            folder_url = str(payload.get("folder_url") or "")
        except Exception:
            folder_url = ""
    else:
        params = parse_qs(raw_body.decode("utf-8", errors="ignore"))
        folder_url = (params.get("folder_url") or [""])[0]

    ok, message = complete_session(token, folder_url)
    wants_json = "application/json" in (content_type or "")
    if wants_json:
        payload = json.dumps({"ok": ok, "error": None if ok else message})
        return (200 if ok else 400), "application/json", payload

    if ok:
        body = _page_shell(
            "Done",
            "<p>Folder saved. You can close this tab and return to Slack.</p>",
        )
        return 200, "text/html; charset=utf-8", body

    body = _page_shell("Could not save", f"<p>{html.escape(message)}</p>")
    return 400, "text/html; charset=utf-8", body


def _page_shell(title: str, inner_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --ink: #1c1917;
      --muted: #78716c;
      --panel: #fafaf9;
      --line: #e7e5e4;
      --accent: #0f766e;
    }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 500px at 10% -10%, #ccfbf1 0%, transparent 55%),
        radial-gradient(900px 400px at 100% 0%, #fef3c7 0%, transparent 50%),
        #f5f5f4;
      min-height: 100vh;
    }}
    main {{
      max-width: 560px;
      margin: 0 auto;
      padding: 48px 20px;
    }}
    h1 {{ font-size: 1.45rem; margin: 0 0 12px; }}
    h2 {{ font-size: 1.05rem; margin: 0 0 8px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 18px;
      margin: 16px 0;
    }}
    label {{ display: block; font-size: 0.9rem; margin-bottom: 6px; }}
    input {{
      width: 100%;
      box-sizing: border-box;
      padding: 10px 12px;
      border: 1px solid var(--line);
      margin-bottom: 12px;
      font: inherit;
    }}
    button {{
      background: var(--accent);
      color: white;
      border: 0;
      padding: 10px 14px;
      font: inherit;
      cursor: pointer;
    }}
    .muted {{ color: var(--muted); font-size: 0.9rem; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    {inner_html}
  </main>
</body>
</html>
"""
