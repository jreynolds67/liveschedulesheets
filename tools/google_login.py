#!/usr/bin/env python3
"""Sign in to Google once and write a credentials file for the web UI.

For orgs whose policy blocks service-account key creation: instead of a key,
the service reads the sheet as a Google user, using a refresh token.

Run this on your own computer (it opens a browser), standard library only:

    python3 tools/google_login.py client_secret.json

`client_secret.json` is an OAuth client ID of type "Desktop app" downloaded
from Google Cloud Console. It writes `google-user-credentials.json`; upload
that in the web UI's Google Sheet card (Upload key). Keep it secret: it grants
read-only access to every sheet the signed-in account can see.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "openid",
    "email",  # only so the web UI can show which account is signed in
]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
OUT_FILE = "google-user-credentials.json"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        client = json.load(fh)
    client = client.get("installed") or client.get("web")
    if not client:
        print("That isn't an OAuth client file (expected an \"installed\" section). "
              "Create an OAuth client ID of type Desktop app and download its JSON.")
        return 1

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and go back to the terminal.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    url = client.get("auth_uri", AUTH_URI) + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # always return a refresh token
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Opening your browser to sign in. If it doesn't open, visit:\n\n  " + url + "\n")
    webbrowser.open(url)
    while not result:
        server.handle_request()
    server.server_close()

    if "error" in result:
        print(f"Sign-in failed: {result['error']}")
        return 1

    token_uri = client.get("token_uri", TOKEN_URI)
    body = urllib.parse.urlencode({
        "code": result["code"],
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(token_uri, data=body)) as resp:
        tokens = json.load(resp)
    if not tokens.get("refresh_token"):
        print("Google didn't return a refresh token; try again.")
        return 1

    account = None
    if tokens.get("id_token"):
        # Display only (came straight from Google over TLS), so no signature check.
        payload = tokens["id_token"].split(".")[1]
        account = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email")

    creds = {
        "type": "authorized_user",
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "refresh_token": tokens["refresh_token"],
        "token_uri": token_uri,
        "account": account,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(creds, fh, indent=2)
    print(f"Wrote {OUT_FILE}" + (f" for {account}" if account else "")
          + ". Upload it in the web UI (Google Sheet -> Upload key), then delete it here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
