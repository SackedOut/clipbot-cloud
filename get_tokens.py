"""One-time: log in to both Google accounts and store the keys as GitHub secrets.

Nothing is printed or saved to disk; values go straight into GitHub's encrypted secrets.
"""
import json
import subprocess
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

DRIVE_EMAIL = "luhsac04@gmail.com"
YOUTUBE_EMAIL = "connormarsac69@gmail.com"

repo = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                      capture_output=True, text=True, check=True).stdout.strip()


def secret(name, value):
    subprocess.run(["gh", "secret", "set", name, "--repo", repo], input=value.encode(), check=True)
    print(f"   saved {name}", flush=True)


def login(scopes, email, why):
    print(f"\n>>> A browser tab will open. Sign in as {email} ({why}).")
    print("    If you see 'Google hasn't verified this app': Advanced > Go to ClipBot > Continue.")
    flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", scopes)
    creds = flow.run_local_server(port=0, open_browser=False, prompt="consent select_account",
                                  access_type="offline", login_hint=email,
                                  authorization_prompt_message="AUTH_URL {url}")
    if not creds.refresh_token:
        sys.exit("No refresh token came back. Run this again.")
    return creds.refresh_token


client = json.load(open("client_secret.json"))
client = client.get("installed") or client.get("web")
secret("GOOGLE_CLIENT_ID", client["client_id"])
secret("GOOGLE_CLIENT_SECRET", client["client_secret"])
secret("DRIVE_REFRESH_TOKEN",
       login(["https://www.googleapis.com/auth/drive"], DRIVE_EMAIL, "the Drive with your To Post folder"))
secret("YOUTUBE_REFRESH_TOKEN",
       login(["https://www.googleapis.com/auth/youtube.upload"], YOUTUBE_EMAIL, "the account that owns your YouTube channel"))
print("\nGoogle logins saved.")
