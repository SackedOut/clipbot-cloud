"""AI editing pass: Gemini watches the clip and decides the trim, the callouts and the title."""
import json
import logging
import os
import time
from typing import List

from pydantic import BaseModel, Field

log = logging.getLogger("clipbot")


class Segment(BaseModel):
    start: float = Field(description="seconds from the start of the ORIGINAL clip")
    end: float


class Callout(BaseModel):
    start: float = Field(description="seconds in the ORIGINAL clip when the text appears")
    end: float
    text: str = Field(description="1 to 4 words, ALL CAPS")


class Event(BaseModel):
    time: float = Field(description="seconds in the ORIGINAL clip when it happens")
    what: str = Field(description="short note, e.g. 'headshot kill on Ash', 'round won'")


class Plan(BaseModel):
    game: str
    kills: List[Event] = Field(description="EVERY kill the player (first-person camera) gets, in order")
    big_moments: List[Event] = Field(description="other key moments: clutch start (1vX), defuser plant/defuse, round won, near-death escape")
    title: str = Field(description="2 to 5 word on-screen title, ALL CAPS, e.g. 8 HP CLUTCH")
    description: str = Field(description="one casual sentence for the YouTube description")
    keep: List[Segment]
    callouts: List[Callout]


PROMPT = """You are editing a PS5 gameplay clip ({duration:.1f} seconds) into a punchy YouTube video.
The player is whoever's first-person view we see{player}. Watch the WHOLE clip carefully and return JSON only.

kills: EVERY kill the player gets, with the exact second it happens. Look for: the kill feed entry with the player's name on the left,
  hit/kill markers on the crosshair, "+ points" popups, an enemy dropping right after the player shoots. Do not miss any. Do not list
  teammates' kills or deaths.
big_moments: clutch banners like "1 VS 3", planting/defusing, "ROUND WON"/"ROUND LOST" screens, the player nearly dying.
keep: the parts worth keeping as start/end seconds of the ORIGINAL clip, in order.
  - EVERY kill must be inside a kept part, with about 3-4 s of lead-in (the aim/peek) and 1-2 s after.
  - Kills close together go in one part so the fight plays out. Keep the round-win screen briefly if the round is won.
  - Cut everything else: walking, waiting, droning, menus, spectating teammates, kill cams, replays, scoreboards, next round.
title: 2-5 words, ALL CAPS, about what actually happened, using the real kill count. Examples: "CLEAN 3K ON DEFENSE", "1V3 CLUTCH", "4K ACE".
  If it's 5 kills in R6 it's an "ACE". No hashtags, no emojis, no swearing.
description: one casual sentence about the play (mention the map if you can see it).
callouts: 3-8 short on-screen captions (1-4 words, ALL CAPS) timed to key moments, ORIGINAL seconds, each 1-2.5 s, never overlapping.
game: the game's name.
"""


def analyze(path, duration, cfg):
    """Returns a cleaned-up plan dict, or None if the AI step isn't available."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not cfg.get("smart_edit", True):
        return None
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    f = client.files.upload(file=str(path))
    try:
        for _ in range(120):
            if f.state and f.state.name != "PROCESSING":
                break
            time.sleep(3)
            f = client.files.get(name=f.name)
        if f.state and f.state.name == "FAILED":
            raise RuntimeError("Gemini could not process the video")

        last_err = None
        for model in candidate_models(client, cfg):
            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model=model,
                        contents=[types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=f.mime_type),
                                             video_metadata=types.VideoMetadata(fps=float(cfg.get("ai_fps", 3)))),
                                  PROMPT.format(duration=duration,
                                                player=f' (PSN name: {cfg["player_name"]})' if cfg.get("player_name") else "")],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=Plan,
                            temperature=0.4,
                        ),
                    )
                    plan = json.loads(resp.text)
                    log.info("  AI plan from %s", model)
                    return clean(plan, duration)
                except Exception as e:  # rate limit, model not available, bad json...
                    last_err = e
                    msg = str(e)
                    log.warning("  %s attempt %d failed: %s", model, attempt + 1, msg[:200])
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        time.sleep(20 * (attempt + 1))
                        continue
                    break  # try next model
        log.warning("  AI edit unavailable, falling back to plain edit (%s)", str(last_err)[:200])
        return None
    finally:
        try:
            client.files.delete(name=f.name)
        except Exception:
            pass


def candidate_models(client, cfg):
    """Configured models first, then whatever current Flash models this key can use."""
    wanted = list(cfg.get("gemini_models") or [])
    found = []
    try:
        for m in client.models.list():
            name = (m.name or "").split("/")[-1]
            acts = getattr(m, "supported_actions", None) or []
            low = name.lower()
            if "flash" in low and not any(x in low for x in ("lite", "image", "tts", "audio", "live", "embedding", "thinking-exp")) \
                    and (not acts or "generateContent" in acts):
                found.append(name)
    except Exception as e:
        log.warning("  could not list Gemini models: %s", str(e)[:150])
    # newest first: stable names before previews
    found.sort(key=lambda n: ("preview" in n or "exp" in n, [-int(x) if x.isdigit() else 0 for x in n.replace("-", ".").split(".")]))
    seen, out = set(), []
    for n in wanted + found:
        if n not in seen:
            seen.add(n); out.append(n)
    log.info("  Gemini models to try: %s", out[:6])
    return out[:6]


def clean(plan, duration):
    segs = []
    kills = sorted(float(k["time"]) for k in plan.get("kills") or [] if 0 <= float(k.get("time", -1)) <= duration)
    for t in kills:  # guarantee each kill is shown with lead-in
        segs.append([max(0.0, t - 4.0), min(duration, t + 2.0)])
    for m in plan.get("big_moments") or []:
        if "won" in str(m.get("what", "")).lower():
            t = float(m["time"]); segs.append([max(0.0, t - 0.5), min(duration, t + 2.0)])
    for s in plan.get("keep") or []:
        a, b = max(0.0, float(s["start"])), min(duration, float(s["end"]))
        if b - a >= 0.8:
            segs.append([a, b])
    segs.sort()
    merged = []
    for a, b in segs:
        if merged and a <= merged[-1][1] + 3.0:   # join parts that are close so fights flow
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if not merged or sum(b - a for a, b in merged) < 3:
        merged = [[0.0, duration]]

    calls = []
    for c in sorted(plan.get("callouts") or [], key=lambda c: float(c["start"])):
        text = " ".join(str(c.get("text", "")).upper().split()[:4])
        a = float(c["start"])
        b = max(a + 0.8, min(float(c["end"]), a + 2.5))
        if text and (not calls or a >= calls[-1]["end"]):
            calls.append({"start": a, "end": b, "text": text})

    title = " ".join(str(plan.get("title", "")).upper().split()[:6]).strip(" #")
    log.info("  AI found %d kills at %s", len(kills), [round(k, 1) for k in kills])
    return {
        "game": str(plan.get("game", "")).strip()[:60],
        "title": title[:60],
        "description": str(plan.get("description", "")).strip()[:300],
        "keep": merged,
        "callouts": calls,
    }


def remap(t, keep):
    """Original-clip time -> time in the trimmed video (None if that moment was cut)."""
    off = 0.0
    for a, b in keep:
        if a <= t <= b:
            return off + (t - a)
        off += b - a
    return None


def callout_words(calls, keep):
    """Turn callouts into caption 'words' (each word pops in, line stays together)."""
    words = []
    for gi, c in enumerate(calls):
        s = remap(c["start"], keep)
        if s is None:
            continue
        e = remap(c["end"], keep) or s + (c["end"] - c["start"])
        e = max(e, s + 0.8)
        toks = c["text"].split()
        step = min(0.3, (e - s) / (len(toks) + 1))
        for i, w in enumerate(toks):
            ws = s + i * step
            we = (s + (i + 1) * step) if i + 1 < len(toks) else e
            words.append({"w": w, "s": ws, "e": we, "g": gi})
    return words
