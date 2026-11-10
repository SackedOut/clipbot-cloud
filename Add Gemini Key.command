#!/bin/bash
# 1) In AI Studio click "Copy key"   2) double-click this file. That's it.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")" || exit 1
K="$(pbpaste | tr -d '[:space:]')"
if [ ${#K} -lt 30 ]; then echo "Clipboard doesn't hold a key. Click 'Copy key' in AI Studio first."; exit 1; fi
printf '%s' "$K" | gh secret set GEMINI_API_KEY --repo "$(gh repo view --json nameWithOwner -q .nameWithOwner)" && echo "Gemini key saved. AI editing is on."
printf '' | pbcopy; unset K
gh workflow run clipbot.yml >/dev/null 2>&1 || true
echo "Done. You can close this window."
