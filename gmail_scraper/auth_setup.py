"""
One-time OAuth setup: mint a Gmail refresh token you can store as a secret.

Run this **on your own machine** (not in CI) — it opens a browser so you can
grant the scraper read-only access to the mailbox.

    pip install -r gmail_scraper/requirements.txt
    python -m gmail_scraper.auth_setup --client-secret ~/Downloads/client_secret.json

Where client_secret.json comes from:
    Google Cloud Console -> APIs & Services
      1. Enable the **Gmail API** for the project
      2. OAuth consent screen -> External (or Internal for Workspace) ->
         add your address under Test users if the app stays unpublished
      3. Credentials -> Create credentials -> OAuth client ID ->
         Application type: **Desktop app** -> Download JSON

It prints GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN. Put
those three in .env locally and in GitHub Secrets for the workflow. The refresh
token does not expire as long as the OAuth app stays published/in use and you
don't revoke access.
"""
import argparse
import json
import sys

from gmail_scraper import config


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint a Gmail refresh token")
    ap.add_argument("--client-secret", required=True,
                    help="Path to the OAuth client JSON downloaded from Google Cloud")
    ap.add_argument("--manual", action="store_true",
                    help="Headless flow: print a URL, paste back the redirect URL")
    args = ap.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    with open(args.client_secret, encoding="utf-8") as fh:
        client_config = json.load(fh)

    flow = InstalledAppFlow.from_client_config(client_config, config.SCOPES)

    if args.manual:
        flow.redirect_uri = "http://localhost:1/"
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true")
        print("\n1. Open this URL and approve access:\n")
        print(f"   {auth_url}\n")
        print("2. The browser will fail to load a localhost page — that is fine.")
        print("   Copy the FULL url from the address bar and paste it here.\n")
        response = input("Redirect URL: ").strip()
        flow.fetch_token(authorization_response=response)
    else:
        flow.run_local_server(port=0, access_type="offline", prompt="consent")

    creds = flow.credentials
    if not creds.refresh_token:
        print("No refresh token returned. Revoke the app's access at "
              "https://myaccount.google.com/permissions and re-run.", file=sys.stderr)
        return 1

    print("\nAdd these to .env (and to GitHub Secrets for the workflow):\n")
    print(f"GMAIL_CLIENT_ID={creds.client_id}")
    print(f"GMAIL_CLIENT_SECRET={creds.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
