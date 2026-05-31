"""
Generate a fresh Google OAuth token for GitHub Secrets.

Usage:
1. Put OAuth client credentials at credentials.json.
2. Run: python generate_google_token.py
3. Sign in and approve Gmail/Sheets access in the browser.
4. Copy the printed JSON into the GitHub Secret GOOGLE_TOKEN_JSON.
"""

import argparse
import json

from google_auth_oauthlib.flow import InstalledAppFlow

from google_auth import SCOPES


def main():
    parser = argparse.ArgumentParser(description="Generate Google OAuth token JSON.")
    parser.add_argument("--credentials", default="credentials.json", help="OAuth client credentials file")
    parser.add_argument("--output", default="token.json", help="Where to save the generated token")
    args = parser.parse_args()

    flow = InstalledAppFlow.from_client_secrets_file(args.credentials, SCOPES)
    creds = flow.run_local_server(port=0)
    token_json = creds.to_json()

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(token_json)

    compact = json.dumps(json.loads(token_json), separators=(",", ":"))
    print("\nGenerated token:")
    print(args.output)
    print("\nCopy this full line into GitHub Secret GOOGLE_TOKEN_JSON:")
    print(compact)


if __name__ == "__main__":
    main()
