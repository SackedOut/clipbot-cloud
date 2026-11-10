"""Quick check so empty runs finish in seconds. Prints has_clips=true/false."""
import time
from datetime import datetime

import cloud_bot

drive = cloud_bot.drive_service()
import yaml
cfg = yaml.safe_load(open(cloud_bot.HERE / "config.yaml"))
inbox = cloud_bot.folder_id(drive, (cfg.get("drive_folders") or {}).get("inbox", "To Post"))
vids = cloud_bot.list_videos(drive, inbox)
# only count clips that finished uploading at least a minute ago
ready = [v for v in vids
         if time.time() - datetime.fromisoformat(v["modifiedTime"].replace("Z", "+00:00")).timestamp() > 60]
print(f"has_clips={'true' if ready else 'false'}")
