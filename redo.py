"""Move a clip from Posted back to To Post so it gets edited again. Usage: python redo.py "<file name or part of it>" """
import sys
import yaml
import cloud_bot

name = " ".join(sys.argv[1:]).strip()
if not name:
    sys.exit(0)
cfg = yaml.safe_load(open(cloud_bot.HERE / "config.yaml"))
f = cfg.get("drive_folders") or {}
drive = cloud_bot.drive_service()
posted = cloud_bot.folder_id(drive, f.get("posted", "Posted"))
inbox = cloud_bot.folder_id(drive, f.get("inbox", "To Post"))
q = f"'{posted}' in parents and trashed=false and name contains '{name.replace(chr(39), '')}'"
hits = drive.files().list(q=q, fields="files(id,name)").execute().get("files", [])
for h in hits:
    drive.files().update(fileId=h["id"], addParents=inbox, removeParents=posted, fields="id").execute()
    print(f"redo: moved {h['name']} back to To Post")
if not hits:
    print(f"redo: nothing in Posted matching '{name}'")
