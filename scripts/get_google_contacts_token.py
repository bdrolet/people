"""One-off: run the OAuth flow for the People API and print a refresh token.
Reuses schedule's OAuth client (same Google account). Then:
  gcloud secrets versions add google-contacts-refresh-token --data-file=- <<< "$TOKEN"
Usage: GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/get_google_contacts_token.py
"""

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

client_config = {
    "installed": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(
    client_config, scopes=["https://www.googleapis.com/auth/contacts"]
)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print("\nRefresh token (GOOGLE_REFRESH_TOKEN / google-contacts-refresh-token):")
print(creds.refresh_token)
