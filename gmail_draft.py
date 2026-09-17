"""Create Gmail drafts locally via the Gmail API (no Claude session required).

One-time setup:
  1. In Google Cloud Console, create/select a project and enable the Gmail API.
  2. OAuth consent screen: External, add yourself as a test user (Testing mode is fine).
  3. Credentials > Create Credentials > OAuth client ID > Application type: Desktop app.
  4. Download the JSON and save it as credentials.json next to this script.
  5. Run this script directly once (`.venv/bin/python3 gmail_draft.py`) to complete the
     browser consent flow; it stores a refresh token in token.json (gitignored) so
     future runs (e.g. from a scheduled job) don't need a browser.

Scope is gmail.compose only: create/edit drafts, cannot read your inbox or send mail
without you clicking Send yourself.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
HERE = Path(__file__).parent
TOKEN_PATH = HERE / "token.json"
CREDS_PATH = HERE / "credentials.json"


def get_service():
    if not CREDS_PATH.exists():
        raise SystemExit(
            f"Missing {CREDS_PATH}. Follow the setup steps in this file's docstring first."
        )

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def create_draft(to_addr: str, subject: str, body_text: str) -> dict:
    service = get_service()
    message = MIMEText(body_text)
    message["to"] = to_addr
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()


if __name__ == "__main__":
    draft = create_draft(
        "shahidmehmood373@gmail.com",
        "Gmail draft test",
        "If you can see this as a draft in Gmail, OAuth is set up correctly.",
    )
    print(f"Created draft id={draft.get('id')} - check your Gmail Drafts folder.")
