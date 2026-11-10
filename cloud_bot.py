#!/usr/bin/env python3
"""ClipBot (cloud): Google Drive "To Post" folder -> AI edit -> YouTube.

Runs on GitHub Actions on a schedule. Needs these repo secrets:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   (OAuth desktop client)
  DRIVE_REFRESH_TOKEN                      (Google account with the To Post folder)
  YOUTUBE_REFRESH_TOKEN                    (Google account that owns the channel)
  GEMINI_API_KEY                           (AI editing; optional, falls back to plain edit)
"""
import argparse
import io
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import yaml

import editor
import smart

HERE = Path(__file__).resolve().parent
log = logging.getLogger("clipbot")
FOLDER = "application/vnd.google-apps.folder"
START = time.time()


# ----------------------------------------------------------------- google
def creds(token_env, scopes):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    c = Credentials(None, refresh_token=os.environ[token_env],
                    client_id=os.environ["GOOGLE_CLIENT_ID"],
                    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                    token_uri="https://oauth2.googleapis.com/token", scopes=scopes)
    c.refresh(Request())
    return c


def drive_service():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds("DRIVE_REFRESH_TOKEN",
                 ["https://www.googleapis.com/auth/drive"]), cache_discovery=False)


def youtube_service():
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds("YOUTUBE_REFRESH_TOKEN",
                 ["https://www.googleapis.com/auth/youtube.upload"]), cache_discovery=False)


def folder_id(drive, name):
    q = (f"name = '{name}' and mimeType = '{FOLDER}' and 'root' in parents and trashed = false")
    hits = drive.files().list(q=q, fields="files(id)", pageSize=5).execute().get("files", [])
    if hits:
        return hits[0]["id"]
    return drive.files().create(body={"name": name, "mimeType": FOLDER, "parents": ["root"]},
                                fields="id").execute()["id"]


def list_videos(drive, parent):
    q = f"'{parent}' in parents and trashed = false and mimeType contains 'video/'"
    files = drive.files().list(q=q, orderBy="createdTime", pageSize=50,
                               fields="files(id,name,size,modifiedTime)").execute()
    return files.get("files", [])


def count_children(drive, parent):
    n, token = 0, None
    while True:
        r = drive.files().list(q=f"'{parent}' in parents and trashed = false", pageToken=token,
                               pageSize=1000, fields="nextPageToken,files(id)").execute()
        n += len(r.get("files", []))
        token = r.get("nextPageToken")
        if not token:
            return n


def download(drive, fid, dest):
    from googleapiclient.http import MediaIoBaseDownload
    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, drive.files().get_media(fileId=fid), chunksize=32 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()


def upload_to_drive(drive, path, parent):
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)
    return drive.files().create(body={"name": Path(path).name, "parents": [parent]},
                                media_body=media, fields="id").execute()["id"]


def upload_text_to_drive(drive, path, parent):
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(path), mimetype="text/plain")
    drive.files().create(body={"name": Path(path).name, "parents": [parent]},
                         media_body=media, fields="id").execute()


def move(drive, fid, src, dst):
    drive.files().update(fileId=fid, addParents=dst, removeParents=src, fields="id").execute()


def post_youtube(yt, path, title, description, tags, cfg, short):
    from googleapiclient.http import MediaFileUpload
    if short and "#shorts" not in title.lower() and len(title) <= 90:
        title = f"{title} #shorts"
    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": tags[:30], "categoryId": str(cfg.get("category_id", "20"))},
        "status": {"privacyStatus": cfg.get("privacy", "public"),
                   "selfDeclaredMadeForKids": bool(cfg.get("made_for_kids", False))},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), chunksize=8 * 1024 * 1024,
                                                        resumable=True, mimetype="video/mp4"))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ----------------------------------------------------------------- editing
def cut(src, keep, out):
    """Keep only the chosen segments (re-encoded at high quality for the main render)."""
    if len(keep) == 1 and keep[0][0] < 0.05:
        dur = editor.probe(src)["duration"]
        if keep[0][1] >= dur - 0.05:
            return src
    parts, fc = [], ""
    for i, (a, b) in enumerate(keep):
        fc += (f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];"
               f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}];")
        parts.append(f"[v{i}][a{i}]")
    fc += "".join(parts) + f"concat=n={len(keep)}:v=1:a=1[v][a]"
    editor.run(["ffmpeg", "-y", "-hide_banner", "-i", str(src), "-filter_complex", fc,
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "14", "-c:a", "aac", "-b:a", "256k", str(out)])
    return out


AUTO = re.compile(r"^(share_|img_|vid_|trim|video|clip|rpreplay|screenrecording)|^[0-9a-f]{8}-|^\d{6,}|_\d{12,14}$", re.I)


def process(drive, yt, f, folders, cfg, color_idx, tmp):
    src = Path(tmp, "src" + (Path(f["name"]).suffix or ".mp4"))
    log.info("Downloading %s", f["name"])
    download(drive, f["id"], src)
    if editor.probe(src)["has_audio"] is False:
        log.info("  (no audio track)")
    info0 = editor.probe(src)

    pal = cfg.get("highlight_palette") or ["&H0000FF66"]
    cfg = dict(cfg, caption_highlight_color=pal[color_idx % len(pal)])

    plan = smart.analyze(src, info0["duration"], cfg)
    stem = Path(f["name"]).stem
    user_title = None if AUTO.search(stem) else stem.replace("_", " ").strip()

    if plan:
        keep = plan["keep"]
        log.info("  keep %s | %d callouts | AI title: %s", [[round(a, 1), round(b, 1)] for a, b in keep],
                 len(plan["callouts"]), plan["title"])
        clip = cut(src, keep, Path(tmp, "cut.mp4"))
        words = smart.callout_words(plan["callouts"], keep) if cfg.get("ai_callouts", False) else []
    else:
        keep, clip, words = [[0.0, info0["duration"]]], src, []

    info = editor.probe(clip)
    game = (plan or {}).get("game") or cfg.get("default_game", "")
    title = (user_title or (plan or {}).get("title") or editor.make_title("clip_000000000000", cfg)).upper()
    vertical = info["duration"] <= float(cfg.get("shorts_max_seconds", 60))

    # captions only when someone is actually talking
    if cfg.get("captions", True) and info["has_audio"]:
        try:
            speech = editor.transcribe(clip, cfg)
            busy = [(w["s"], w["e"]) for w in words]
            speech = [w for w in speech if not any(a - 0.2 < w["s"] < b + 0.2 for a, b in busy)]
            words = sorted(words + speech, key=lambda w: w["s"])
            log.info("  %d speech words", len(speech))
        except Exception as e:
            log.warning("  speech captions skipped: %s", e)

    out = Path(tmp, re.sub(r"[^\w\- ]", "", title).strip().replace(" ", "_")[:60] + ("_short" if vertical else "_video") + ".mp4")
    editor.render(clip, out, cfg, vertical, title, words, info)
    log.info("  rendered %s (%.0fs, %s)", out.name, info["duration"], "Short" if vertical else "Video")

    upload_to_drive(drive, out, folders["finished"])

    # YouTube title / description / hashtags
    yt_title = f"{title.title()} | {game}" if game and game.lower() not in title.lower() else title.title()
    if vertical and "#shorts" not in yt_title.lower() and len(yt_title) <= 90:
        yt_title = f"{yt_title} #Shorts"
    tags = list(dict.fromkeys(list(cfg.get("tags") or []) + ([game] if game else [])))
    hashtags = list(dict.fromkeys((["#Shorts"] if vertical else []) + list(cfg.get("hashtags") or [])))
    desc = cfg.get("description", "{title}").format(title=yt_title.replace(" #Shorts", ""), game=game or "")
    if plan and plan.get("description"):
        desc = plan["description"] + "\n\n" + desc
    desc = desc.rstrip() + "\n\n" + " ".join(hashtags)
    preview = (f"TITLE:\n{yt_title}\n\nDESCRIPTION:\n{desc}\n\nTAGS:\n{', '.join(tags)}\n\n"
               f"FORMAT: {'Short' if vertical else 'Video'} ({info['duration']:.0f}s)\n"
               f"CAPTIONS: {'yes, speech found' if words else 'none (no one talking)'}\n")
    log.info("  YouTube preview:\n%s", preview)
    txt = Path(tmp, out.stem + "_youtube.txt")
    txt.write_text(preview)
    upload_text_to_drive(drive, txt, folders["finished"])

    if cfg.get("upload", True):
        vid = post_youtube(yt, out, yt_title, desc, tags, cfg, False)
        log.info("  POSTED https://youtu.be/%s", vid)
    else:
        log.info("  upload is off: saved to ClipBot Finished only")
    move(drive, f["id"], folders["inbox"], folders["posted"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    for noisy in ("httpx", "googleapiclient", "google_genai", "faster_whisper", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    with open(HERE / "config.yaml") as fh:
        cfg = yaml.safe_load(fh)
    if args.no_upload:
        cfg["upload"] = False

    drive = drive_service()
    names = cfg.get("drive_folders", {})
    folders = {k: folder_id(drive, names.get(k, d)) for k, d in
               (("inbox", "To Post"), ("posted", "Posted"), ("failed", "Failed"), ("finished", "ClipBot Finished"))}

    from datetime import datetime
    vids = [f for f in list_videos(drive, folders["inbox"])
            if time.time() - datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00")).timestamp() > 60]
    if not vids:
        log.info("Nothing in To Post.")
        return
    yt = youtube_service() if cfg.get("upload", True) else None
    color_idx = count_children(drive, folders["posted"])
    budget = int(cfg.get("max_uploads_per_run", 3))
    for f in vids[:budget]:
        if time.time() - START > 45 * 60:
            log.info("Out of time this run; the rest go next run.")
            break
        with tempfile.TemporaryDirectory() as tmp:
            try:
                process(drive, yt, f, folders, cfg, color_idx, tmp)
                color_idx += 1
            except Exception as e:
                log.exception("FAILED %s: %s", f["name"], e)
                if "quota" in str(e).lower():
                    log.error("YouTube quota hit; will retry next run.")
                    return
                move(drive, f["id"], folders["inbox"], folders["failed"])


if __name__ == "__main__":
    main()
