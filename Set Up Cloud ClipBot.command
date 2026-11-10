#!/bin/bash
# One-time setup for ClipBot Cloud. No typing needed: logins happen in Chrome.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")" || exit 1
exec > >(tee setup.log) 2>&1
set -e

echo "=== 1/5 Gemini key (from clipboard) ==="
GEMINI_KEY="$(pbpaste | tr -d '[:space:]')"
if [ ${#GEMINI_KEY} -lt 30 ] || [[ "$GEMINI_KEY" == *"/"* ]] || [[ "$GEMINI_KEY" == *" "* ]]; then
  GEMINI_KEY=""
  echo "   no key on clipboard yet - skipping (run 'Add Gemini Key.command' later)"
else
  printf '' | pbcopy
  echo "   got it (clipboard cleared)"
fi

echo "=== 2/5 GitHub login ==="
command -v gh >/dev/null || (yes | brew install gh)
if ! gh auth status >/dev/null 2>&1; then
  gh auth login --hostname github.com --git-protocol https --web </dev/null
fi
gh auth setup-git
echo "   GitHub OK as $(gh api user -q .login)"

echo "=== 3/5 Creating your GitHub repo (clipbot-cloud) ==="
[ -f client_secret.json ] || { echo "client_secret.json missing"; exit 1; }
if [ ! -d .git ]; then
  git init -q -b main
  git -c user.name="ClipBot" -c user.email="clipbot@users.noreply.github.com" add -A
  git -c user.name="ClipBot" -c user.email="clipbot@users.noreply.github.com" commit -qm "ClipBot Cloud"
fi
gh repo view clipbot-cloud >/dev/null 2>&1 || gh repo create clipbot-cloud --public --source . --push
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "   repo: $REPO"
if [ -n "$GEMINI_KEY" ]; then
  printf '%s' "$GEMINI_KEY" | gh secret set GEMINI_API_KEY --repo "$REPO"; echo "   saved GEMINI_API_KEY"
fi
unset GEMINI_KEY

echo "=== 4/5 Google logins ==="
PY=../clipbot/.venv/bin/python
"$PY" -c "import google_auth_oauthlib" 2>/dev/null || "$PY" -m pip install -q google-auth-oauthlib
"$PY" -u get_tokens.py

echo "=== 5/5 Switching over to the cloud ==="
../clipbot/install_schedule.sh off || true
gh workflow run clipbot.yml --repo "$REPO" >/dev/null 2>&1 && echo "First cloud run started." || true
echo ""
echo "=== ALL DONE. ClipBot now runs in the cloud, Mac on or off. ==="
