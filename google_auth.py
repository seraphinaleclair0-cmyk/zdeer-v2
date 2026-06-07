import json
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

def get_creds(
    token_json="token.json",
    credentials_json="credentials.json",
    token_env_var="GOOGLE_TOKEN_JSON",
    service_account_env_var="GOOGLE_SERVICE_ACCOUNT_JSON",
) -> Credentials:
    service_account_env = os.environ.get(service_account_env_var)
    if service_account_env:
        return service_account.Credentials.from_service_account_info(
            json.loads(service_account_env),
            scopes=SCOPES,
        )

    creds = None
    token_env = os.environ.get(token_env_var)
    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env), SCOPES)
    elif os.path.exists(token_json):
        creds = Credentials.from_authorized_user_file(token_json, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_json, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_json, "w") as f:
            f.write(creds.to_json())
    return creds
