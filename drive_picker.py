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


def complete_session(
    token: str,
    folder_url: str,
    access_token: str | None = None,
) -> tuple[bool, str]:
    """
    Persist the selected folder onto the project and share it with the agent SAs.
    Returns (ok, message).
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

    share_ok, share_notes = project_router.share_folder_with_service_accounts(
        folder_id,
        access_token=(access_token or "").strip() or None,
    )
    note = ""
    if share_notes:
        note = " " + "; ".join(share_notes)
    if not share_ok:
        # Folder is still saved; permissions may need a manual share.
        print(
            f"[PICKER] Folder saved for {session.project_id} but sharing incomplete:{note}",
            flush=True,
        )
        with _lock:
            session.completed = True
            session.folder_url = normalized
        return True, (
            "Folder saved, but automatic sharing did not fully succeed."
            f"{note} "
            "You may need to share the folder manually with the service accounts."
        )

    with _lock:
        session.completed = True
        session.folder_url = normalized
    return True, "Folder saved and shared with the agent service accounts."


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
    oauth_enabled = bool(client_id)

    project_name = html.escape(session.project_name)
    safe_token = html.escape(token)

    if not oauth_enabled:
        body = _page_shell(
            f"Link Drive folder — {project_name}",
            f"""
            <p>Project: <strong>{project_name}</strong></p>
            <div class="card">
              <h2>Paste the Google Drive folder URL</h2>
              <p class="muted">
                Set <code>GOOGLE_OAUTH_CLIENT_ID</code> on Cloud Run to enable automatic
                sharing with the agent service accounts. Until then, paste the URL and
                share the folder manually with those accounts after saving.
              </p>
              <form method="POST" action="/drive-picker/{safe_token}">
                <label for="folder_url">Folder URL</label>
                <input id="folder_url" name="folder_url" type="url" required
                  placeholder="https://drive.google.com/drive/folders/..." />
                <button type="submit">Save folder (no auto-share)</button>
              </form>
              <p class="muted">This link expires in 15 minutes.</p>
            </div>
            """,
        )
        return 200, "text/html; charset=utf-8", body

    picker_button = ""
    if picker_enabled:
        picker_button = """
          <button type="button" id="open-picker">Choose folder in Drive</button>
        """

    body = _page_shell(
        f"Link Drive folder — {project_name}",
        f"""
        <p>Project: <strong>{project_name}</strong></p>
        <div class="card">
          <h2>Link a Google Drive folder</h2>
          <p>
            Sign in with Google so this page can share the folder with the agent
            service accounts. Then browse or paste a folder URL.
          </p>
          <button type="button" id="google-signin">1. Sign in with Google</button>
          <p id="auth-status" class="muted">Not signed in yet.</p>
          {picker_button}
          <label for="folder_url">Folder URL</label>
          <input id="folder_url" name="folder_url" type="url"
            placeholder="https://drive.google.com/drive/folders/..." />
          <button type="button" id="save-folder">2. Save &amp; share folder</button>
          <p id="picker-status" class="muted"></p>
          <p class="muted">This link expires in 15 minutes.</p>
        </div>
        <script src="https://accounts.google.com/gsi/client" async defer></script>
        {"<script src='https://apis.google.com/js/api.js'></script>" if picker_enabled else ""}
        <script>
          const API_KEY = {json.dumps(api_key)};
          const CLIENT_ID = {json.dumps(client_id)};
          const APP_ID = {json.dumps(app_id)};
          const TOKEN = {json.dumps(token)};
          const PICKER_ENABLED = {json.dumps(picker_enabled)};
          let accessToken = '';

          function setStatus(msg) {{
            document.getElementById('picker-status').textContent = msg || '';
          }}
          function setAuthStatus(msg) {{
            document.getElementById('auth-status').textContent = msg || '';
          }}

          function requestGoogleToken(thenFn) {{
            if (typeof google === 'undefined' || !google.accounts || !google.accounts.oauth2) {{
              setStatus('Google sign-in is still loading — wait a second and try again.');
              return;
            }}
            const client = google.accounts.oauth2.initTokenClient({{
              client_id: CLIENT_ID,
              scope: 'https://www.googleapis.com/auth/drive',
              callback: (resp) => {{
                if (resp.error) {{
                  setAuthStatus(resp.error);
                  setStatus(resp.error);
                  return;
                }}
                accessToken = resp.access_token;
                setAuthStatus('Signed in. You can choose or paste a folder.');
                if (thenFn) thenFn();
              }}
            }});
            client.requestAccessToken();
          }}

          function saveFolder(url) {{
            if (!url) {{
              setStatus('Paste or choose a folder URL first.');
              return;
            }}
            if (!accessToken) {{
              setStatus('Sign in with Google first, then Save again.');
              requestGoogleToken(() => saveFolder(url));
              return;
            }}
            setStatus('Saving and sharing…');
            fetch('/drive-picker/' + TOKEN, {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{folder_url: url, access_token: accessToken}})
            }}).then(r => r.json()).then(data => {{
              if (data.ok) {{
                window.location.reload();
              }} else {{
                setStatus(data.error || data.message || 'Save failed');
              }}
            }}).catch(err => setStatus(String(err)));
          }}

          document.getElementById('google-signin').addEventListener('click', function() {{
            setAuthStatus('Opening Google sign-in…');
            requestGoogleToken();
          }});

          document.getElementById('save-folder').addEventListener('click', function() {{
            const url = (document.getElementById('folder_url').value || '').trim();
            saveFolder(url);
          }});

          if (PICKER_ENABLED) {{
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
                    const url = 'https://drive.google.com/drive/folders/' + id;
                    document.getElementById('folder_url').value = url;
                    saveFolder(url);
                  }}
                }});
              if (APP_ID) picker.setAppId(APP_ID);
              picker.build().setVisible(true);
            }}

            document.getElementById('open-picker').addEventListener('click', function() {{
              const open = () => {{
                setStatus('Choose a folder…');
                gapi.load('picker', {{callback: createPicker}});
              }};
              if (!accessToken) {{
                setAuthStatus('Opening Google sign-in…');
                requestGoogleToken(open);
              }} else {{
                open();
              }}
            }});
          }}
        </script>
        """,
    )
    return 200, "text/html; charset=utf-8", body


def handle_picker_post(token: str, raw_body: bytes, content_type: str) -> tuple[int, str, str]:
    folder_url = ""
    access_token = ""
    if "application/json" in (content_type or ""):
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            folder_url = str(payload.get("folder_url") or "")
            access_token = str(payload.get("access_token") or "")
        except Exception:
            folder_url = ""
            access_token = ""
    else:
        params = parse_qs(raw_body.decode("utf-8", errors="ignore"))
        folder_url = (params.get("folder_url") or [""])[0]
        access_token = (params.get("access_token") or [""])[0]

    ok, message = complete_session(token, folder_url, access_token=access_token or None)
    wants_json = "application/json" in (content_type or "")
    if wants_json:
        payload = json.dumps({"ok": ok, "error": None if ok else message, "message": message})
        return (200 if ok else 400), "application/json", payload

    if ok:
        body = _page_shell(
            "Done",
            f"<p>{html.escape(message)}</p><p>You can close this tab and return to Slack.</p>",
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
