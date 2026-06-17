"""
Generate a fresh Google OAuth token for GitHub Secrets.

Usage:
1. Put OAuth client credentials at credentials.json.
2. Run: python generate_google_token.py
3. Sign in and approve Gmail/Sheets access in the browser.
4. Copy the printed JSON into the matching GitHub Secret.
"""

import argparse
import json
from urllib.parse import parse_qs, urlparse
import webbrowser
import wsgiref.simple_server

from google_auth_oauthlib.flow import InstalledAppFlow
from google_auth_oauthlib.flow import _RedirectWSGIApp, _WSGIRequestHandler

from google_auth import SCOPES


def main():
    parser = argparse.ArgumentParser(description="Generate Google OAuth token JSON.")
    parser.add_argument("--credentials", default="credentials.json", help="OAuth client credentials file")
    parser.add_argument("--credentials-from-token", default="", help="Read OAuth client_id/client_secret from an existing token JSON")
    parser.add_argument("--output", default="token.json", help="Where to save the generated token")
    parser.add_argument("--secret-name", default="GOOGLE_TOKEN_JSON", help="GitHub Secret name to print")
    parser.add_argument("--no-open-browser", action="store_true", help="Print the auth URL instead of opening a browser")
    parser.add_argument("--auth-url-output", default="", help="Optional file where the auth URL is written")
    parser.add_argument("--port", type=int, default=0, help="Local OAuth callback port")
    parser.add_argument("--no-pkce", action="store_true", help="Do not add a PKCE code challenge")
    args = parser.parse_args()

    if args.credentials_from_token:
        with open(args.credentials_from_token, encoding="utf-8") as f:
            old_token = json.load(f)
        client_config = {
            "installed": {
                "client_id": old_token["client_id"],
                "client_secret": old_token["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": old_token.get("token_uri", "https://oauth2.googleapis.com/token"),
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(
            client_config,
            SCOPES,
            autogenerate_code_verifier=not args.no_pkce,
        )
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            args.credentials,
            SCOPES,
            autogenerate_code_verifier=not args.no_pkce,
        )
    wsgi_app = _RedirectWSGIApp("The authentication flow has completed. You may close this window.")
    wsgiref.simple_server.WSGIServer.allow_reuse_address = False
    local_server = wsgiref.simple_server.make_server(
        "localhost",
        args.port,
        wsgi_app,
        handler_class=_WSGIRequestHandler,
    )

    try:
        flow.redirect_uri = f"http://localhost:{local_server.server_port}/"
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

        if args.auth_url_output:
            with open(args.auth_url_output, "w", encoding="utf-8") as f:
                f.write(auth_url)

        print("\nOpen this URL in the matching Chrome profile:")
        print(auth_url, flush=True)

        if not args.no_open_browser:
            webbrowser.get().open(auth_url, new=1, autoraise=True)

        local_server.handle_request()
        query = parse_qs(urlparse(wsgi_app.last_request_uri).query)
        if query.get("error"):
            raise RuntimeError(f"OAuth authorization failed: {query['error'][0]}")
        code = query.get("code", [""])[0]
        if not code:
            raise RuntimeError(f"OAuth callback did not include a code: {wsgi_app.last_request_uri}")
        flow.fetch_token(code=code)
        creds = flow.credentials
    finally:
        local_server.server_close()
    token_json = creds.to_json()

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(token_json)

    compact = json.dumps(json.loads(token_json), separators=(",", ":"))
    print("\nGenerated token:")
    print(args.output)
    print(f"\nCopy this full line into GitHub Secret {args.secret_name}:")
    print(compact)


if __name__ == "__main__":
    main()
