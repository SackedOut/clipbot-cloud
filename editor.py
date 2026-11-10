#!/usr/bin/env python3
"""ClipBot: drop PS5 clips in a folder, get captioned, edited YouTube videos posted.

Run once:      python clipbot.py
Dry run:       python clipbot.py --no-upload       (edits only, no posting)
Login only:    python clipbot.py --login           (connect your YouTube channel)
"""
import argparse
import fcntl
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

log = logging.getLogger("clipbot")


# ----------------------------------------------------------------- helpers
def load_config():
    with open(HERE / "config.yaml") as f:
        cfg = yaml.safe_load(f)
    for k in ("inbox_folder", "posted_folder", "failed_folder", "output_folder", "music_folder"):
        if cfg.get(k):
            cfg[k] = Path(os.path.expanduser(cfg[k]))
    return cfg


def run(cmd, cwd=None):
    log.debug("RUN %s", " ".join(map(str, cmd)))
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd[0]}\n{p.stderr[-3000:]}")
    return p.stdout


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)])
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    num, den = (v.get("avg_frame_rate") or "60/1").split("/")
    fps = float(num) / float(den) if float(den) else 60.0
    fps = 60 if fps > 45 else 30
    return {
        "duration": float(info["format"]["duration"]),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": fps,
        "has_audio": has_audio,
    }


def is_stable(path, settle=60):
    """True once a file has stopped syncing/copying."""
    try:
        s1 = path.stat()
        if time.time() - s1.st_mtime < settle:
            return False
        time.sleep(3)
        return path.stat().st_size == s1.st_size and s1.st_size > 0
    except FileNotFoundError:
        return False


def unique_dest(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    i = 2
    while dest.exists():
        dest = folder / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    return dest


# ----------------------------------------------------------------- titles
AUTO_NAME = re.compile(
    r"^(img_|vid_|share_|trim|video|clip|rpreplay|screenrecording)|"
    r"^[0-9a-f]{8}-[0-9a-f]{4}-|^\d{6,}|_\d{12,14}$",
    re.I,
)


def split_ps5_name(stem):
    """PS5 USB exports look like 'Game Name_20241112193022'."""
    m = re.match(r"^(.*?)[ _](\d{12,14})$", stem)
    if m and m.group(1).strip():
        return m.group(1).replace("_", " ").replace("®", "").replace("™", "").strip()
    return None


def make_title(stem, cfg):
    game = split_ps5_name(stem)
    if not game and not AUTO_NAME.search(stem):
        return stem.replace("_", " ").strip()[:95]  # you named it, use it
    game = game or cfg.get("default_game", "PS5")
    tpl = random.choice(cfg.get("title_templates") or ["{game} clip"])
    return tpl.format(game=game)[:95]


# ----------------------------------------------------------------- captions
_whisper = None


def transcribe(path, cfg):
    """Returns a list of {'w','s','e'} words with timestamps."""
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        log.info("Loading Whisper model '%s' (first run downloads it)...", cfg["whisper_model"])
        _whisper = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8")
    # decode with ffmpeg ourselves (16 kHz mono) so we don't depend on PyAV
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
    audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    segments, _ = _whisper.transcribe(
        audio, language=cfg.get("caption_language") or None,
        word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        condition_on_previous_text=False,
    )
    words = []
    for seg in segments:
        # drop Whisper's usual hallucinations on game noise
        if seg.no_speech_prob > 0.6 or seg.avg_logprob < -1.2:
            continue
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append({"w": t, "s": w.start, "e": w.end})
    return words


def censor(word, bad):
    core = re.sub(r"[^\w']", "", word).lower()
    for b in bad:
        if b in core:
            return word[0] + "*" * (len(word) - 1)
    return word


def ass_time(t):
    t = max(0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words, cfg, W, H, vertical, title=None, duration=0):
    font = cfg.get("caption_font", "Arial Black")
    tfont = cfg.get("title_font") or font
    tcolor = cfg.get("title_color", "&H00FFFFFF")
    base = cfg.get("caption_color", "&H00FFFFFF")
    hi = cfg.get("caption_highlight_color", "&H0000E5FF")
    size = 92 if vertical else 72
    margin_v = 440 if vertical else 90
    title_size = int(cfg.get("title_size", 74 if vertical else 60))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},{base},{base},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,{margin_v},1",
        f"Style: Title,{tfont},{title_size},{tcolor},{tcolor},&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,6,3,8,70,70,220,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title and vertical:
        ttxt = ass_escape(title.upper())
        n_hi = int(cfg.get("title_highlight_last_words", 1))
        parts = ttxt.split()
        if 0 < n_hi < len(parts):
            ttxt = " ".join(parts[:-n_hi]) + r" {\c" + hi[1:] + "&}" + " ".join(parts[-n_hi:])
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(duration)},Title,,0,0,0,,{ttxt}")

    bad = [b.lower() for b in cfg.get("caption_censor") or []]
    words = [dict(w, w=censor(w["w"], bad)) for w in words]
    n = max(1, int(cfg.get("caption_words_per_line", 3)))
    # group into short chunks, breaking on long pauses too
    chunks, cur = [], []
    for w in words:
        if cur and cur[-1].get("g") is not None:
            # AI callouts: keep each callout together as one line
            if w.get("g") != cur[-1].get("g"):
                chunks.append(cur)
                cur = []
        elif cur and (len(cur) >= n or w["s"] - cur[-1]["e"] > 0.6 or w.get("g") is not None):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    for chunk in chunks:
        for i, w in enumerate(chunk):
            start = w["s"]
            end = chunk[i + 1]["s"] if i + 1 < len(chunk) else w["e"] + 0.15
            parts = []
            for j, x in enumerate(chunk):
                txt = ass_escape(x["w"].upper())
                if j == i:
                    # active word: highlight color + small pop
                    parts.append(r"{\c" + hi[1:] + r"&\fscx112\fscy112}" + txt + r"{\c" + base[1:] + r"&\fscx100\fscy100}")
                else:
                    parts.append(txt)
            pop = r"{\fad(60,0)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{pop}{' '.join(parts)}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- rendering
def pick_music(cfg):
    folder = cfg.get("music_folder")
    if not folder or not Path(folder).is_dir():
        return None
    tracks = [p for p in Path(folder).iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac"}]
    return random.choice(tracks) if tracks else None


def render(src, out, cfg, vertical, title, words, info):
    """Edit one clip: reframe, punch zoom, fades, captions, audio cleanup."""
    src, out = Path(src).resolve(), Path(out).resolve()
    W, H = (1080, 1920) if vertical else (1920, 1080)
    fps, dur = info["fps"], info["duration"]
    fi, fo = float(cfg.get("fade_in_seconds", 0.25)), float(cfg.get("fade_out_seconds", 0.5))
    fo_start = max(0.0, dur - fo)

    with tempfile.TemporaryDirectory() as tmp:
        ass = build_ass(words, cfg, W, H, vertical,
                        title if cfg.get("show_title_on_shorts", True) else None, dur)
        Path(tmp, "caps.ass").write_text(ass, encoding="utf-8")

        sz = float(cfg.get("shorts_gameplay_zoom", 1.25)) if vertical else 1.0
        fw, fh = (1080, int(608 * sz) // 2 * 2) if vertical else (W, H)
        if cfg.get("intro_punch_zoom", True):
            zoom = (f"zoompan=z='if(lt(in_time,0.4),1.15-0.15*in_time/0.4,1)':d=1:"
                    f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={fw}x{fh}:fps={fps}")
        else:
            zoom = f"fps={fps}"

        if vertical:
            v = (f"[0:v]fps={fps},split=2[bgs][fgs];"
                 f"[bgs]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"gblur=sigma=30,eq=brightness=-0.12:saturation=1.2[bg];"
                 f"[fgs]scale={fw}:{fh}:force_original_aspect_ratio=increase,"
                 f"crop={fw}:{fh},setsar=1,{zoom}[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2-60[vv];")
        else:
            v = (f"[0:v]fps={fps},scale={W}:{H}:force_original_aspect_ratio=decrease,"
                 f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,{zoom}[vv];")
        caps = f",subtitles=caps.ass:fontsdir='{HERE / 'fonts'}'" if (words or (vertical and title)) else ""
        v += (f"[vv]fade=t=in:st=0:d={fi},fade=t=out:st={fo_start:.3f}:d={fo}"
              f"{caps},format=yuv420p[vout]")

        inputs = ["-i", str(src)]
        music = pick_music(cfg)
        if info["has_audio"]:
            a = f"[0:a]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11[ga];"
        else:
            a = f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f}[ga];"
        if music:
            inputs += ["-stream_loop", "-1", "-i", str(music)]
            vol = float(cfg.get("music_volume", 0.12))
            a += (f"[1:a]aresample=48000,volume={vol},atrim=0:{dur:.3f}[mu];"
                  f"[ga][mu]amix=inputs=2:duration=first:normalize=0[am];[am]")
        else:
            a += "[ga]"
        a += f"afade=t=in:st=0:d={fi},afade=t=out:st={fo_start:.3f}:d={fo}[aout]"

        cmd = ["ffmpeg", "-y", "-hide_banner", *inputs,
               "-filter_complex", v + ";" + a,
               "-map", "[vout]", "-map", "[aout]", "-t", f"{dur:.3f}",
               "-c:v", "libx264", "-preset", "medium", "-crf", "19",
               "-profile:v", "high", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
        run(cmd, cwd=tmp)
    return out


def stitch(parts, out, cfg, fps=60):
    """Join already-rendered clips with transitions (for compilations)."""
    T = float(cfg.get("transition_seconds", 0.5))
    kind = cfg.get("transition", "fade")
    durs = [probe(p)["duration"] for p in parts]
    inputs, vchain, achain = [], "", ""
    for i, p in enumerate(parts):
        inputs += ["-i", str(p)]
        vchain += f"[{i}:v]fps={fps},settb=AVTB,format=yuv420p[v{i}];"
        achain += f"[{i}:a]aresample=48000[a{i}];"
    vlast, alast, offset = "v0", "a0", 0.0
    for i in range(1, len(parts)):
        offset += durs[i - 1] - T
        vchain += f"[{vlast}][v{i}]xfade=transition={kind}:duration={T}:offset={offset:.3f}[vx{i}];"
        achain += f"[{alast}][a{i}]acrossfade=d={T}[ax{i}];"
        vlast, alast = f"vx{i}", f"ax{i}"
    fc = (vchain + achain).rstrip(";")
    run(["ffmpeg", "-y", "-hide_banner", *inputs, "-filter_complex", fc,
         "-map", f"[{vlast}]", "-map", f"[{alast}]",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)])
    return out


# ----------------------------------------------------------------- youtube
def youtube_client():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token = HERE / "token.json"
    secret = HERE / "client_secret.json"
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secret.exists():
                raise RuntimeError("client_secret.json missing. See README step 3.")
            if not sys.stdin.isatty():
                raise RuntimeError("YouTube login expired. Run: ./clipbot.sh --login")
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(path, title, cfg, short):
    from googleapiclient.http import MediaFileUpload
    yt = youtube_client()
    desc = cfg.get("description", "{title}").format(title=title)
    tags = list(cfg.get("tags") or [])
    if short:
        if "#shorts" not in title.lower() and len(title) <= 90:
            title = f"{title} #shorts"
        desc += "\n#shorts"
        tags.append("shorts")
    body = {
        "snippet": {"title": title[:100], "description": desc[:4900], "tags": tags,
                    "categoryId": str(cfg.get("category_id", "20"))},
        "status": {"privacyStatus": cfg.get("privacy", "public"),
                   "selfDeclaredMadeForKids": bool(cfg.get("made_for_kids", False))},
    }
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True,
                            mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            log.info("  uploading... %d%%", int(status.progress() * 100))
    vid = resp["id"]
    log.info("  posted: https://youtu.be/%s", vid)
    return vid



# ----------------------------------------------------------------- colors
def next_color(cfg):
    """Rotate the highlight color every video so posts don't all look the same."""
    pal = cfg.get("highlight_palette") or [cfg.get("caption_highlight_color", "&H0000FF66")]
    state = HERE / ".color_index"
    try:
        i = int(state.read_text().strip())
    except Exception:
        i = -1
    i = (i + 1) % len(pal)
    state.write_text(str(i))
    return pal[i]

# ----------------------------------------------------------------- jobs
def process_single(src, cfg, do_upload):
    cfg = dict(cfg, caption_highlight_color=next_color(cfg))
    info = probe(src)
    vertical = info["duration"] <= float(cfg.get("shorts_max_seconds", 60))
    title = make_title(src.stem, cfg)
    log.info("Clip: %s (%.0fs) -> %s, title: %s", src.name, info["duration"],
             "Short" if vertical else "Video", title)
    words = transcribe(src, cfg) if cfg.get("captions", True) and info["has_audio"] else []
    log.info("  %d caption words", len(words))
    out = unique_dest(cfg["output_folder"], f"{src.stem}_{'short' if vertical else 'video'}.mp4")
    render(src, out, cfg, vertical, title, words, info)
    log.info("  edited: %s", out)
    if do_upload:
        upload(out, title, cfg, vertical)


def process_compilation(folder, cfg, do_upload):
    clips = sorted([p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTS],
                   key=lambda p: p.stat().st_mtime)
    if not clips:
        return
    title = folder.name
    cfg = dict(cfg, caption_highlight_color=next_color(cfg))
    log.info("Compilation: %s (%d clips)", title, len(clips))
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, c in enumerate(clips):
            info = probe(c)
            words = transcribe(c, cfg) if cfg.get("captions", True) and info["has_audio"] else []
            part = Path(tmp, f"part{i:03d}.mp4")
            clip_cfg = dict(cfg, fade_in_seconds=0.01, fade_out_seconds=0.01, music_folder="")
            render(c, part, clip_cfg, False, None, words, dict(info, fps=60))
            parts.append(part)
        out = unique_dest(cfg["output_folder"], f"{title}_compilation.mp4")
        if len(parts) == 1:
            shutil.copy(parts[0], out)
        else:
            stitch(parts, out, cfg)
    log.info("  edited: %s", out)
    if do_upload:
        upload(out, title, cfg, short=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true", help="edit only, skip YouTube")
    ap.add_argument("--login", action="store_true", help="connect YouTube and exit")
    ap.add_argument("--file", help="process one file directly (for testing)")
    args = ap.parse_args()

    (HERE / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(HERE / "logs" / "clipbot.log"), logging.StreamHandler()])

    cfg = load_config()
    if args.login:
        youtube_client()
        print("YouTube connected.")
        return

    lock = open(HERE / ".clipbot.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # another run is still working

    do_upload = cfg.get("upload", True) and not args.no_upload
    for k in ("output_folder", "posted_folder", "failed_folder"):
        if cfg[k].parent.is_dir():
            cfg[k].mkdir(exist_ok=True)

    if args.file:
        process_single(Path(args.file), cfg, do_upload)
        return

    inbox = cfg["inbox_folder"]
    if not inbox.parent.is_dir():
        log.error("Drive folder not found (is Google Drive running and signed in?): %s", inbox.parent)
        return
    inbox.mkdir(exist_ok=True)
    comp_root = inbox / "Compilations"
    comp_root.mkdir(exist_ok=True)

    budget = int(cfg.get("max_uploads_per_run", 3))

    # single clips
    for src in sorted(inbox.iterdir(), key=lambda p: p.stat().st_mtime):
        if budget <= 0:
            break
        if src.suffix.lower() not in VIDEO_EXTS or src.name.startswith("."):
            continue
        if not is_stable(src):
            continue
        try:
            process_single(src, cfg, do_upload)
            shutil.move(str(src), unique_dest(cfg["posted_folder"], src.name))
            budget -= 1
        except Exception as e:
            log.exception("FAILED %s: %s", src.name, e)
            if "quota" in str(e).lower():
                log.error("YouTube daily quota hit. Will retry next run.")
                return
            shutil.move(str(src), unique_dest(cfg["failed_folder"], src.name))

    # compilations
    wait = float(cfg.get("compilation_wait_minutes", 30)) * 60
    for folder in sorted(p for p in comp_root.iterdir() if p.is_dir()):
        if budget <= 0:
            break
        clips = [p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTS]
        if not clips or time.time() - max(p.stat().st_mtime for p in clips) < wait:
            continue
        try:
            process_compilation(folder, cfg, do_upload)
            shutil.move(str(folder), unique_dest(cfg["posted_folder"], folder.name))
            budget -= 1
        except Exception as e:
            log.exception("FAILED compilation %s: %s", folder.name, e)
            if "quota" in str(e).lower():
                return
            shutil.move(str(folder), unique_dest(cfg["failed_folder"], folder.name))


if __name__ == "__main__":
    main()
