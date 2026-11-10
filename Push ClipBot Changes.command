#!/bin/bash
# Sends local ClipBot changes to GitHub, then runs the bot once.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")" || exit 1
exec > >(tee push.log) 2>&1
git -c user.name="ClipBot" -c user.email="clipbot@users.noreply.github.com" add -A
git -c user.name="ClipBot" -c user.email="clipbot@users.noreply.github.com" commit -qm "Update ClipBot settings" || echo "(nothing new to commit)"
git push -q origin main && echo "PUSHED"
gh workflow run clipbot.yml && echo "RUN STARTED"
