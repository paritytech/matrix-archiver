#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Matrix → static archive (index.html + room_log.txt)
 – Public, un‑encrypted rooms
 – One‑level threads
CI‑friendly: no interaction, token via env.

ENV (required)
  MATRIX_HS      homeserver URL
  MATRIX_USER    full user id             (e.g. @bot:example.org)
  MATRIX_ROOM    room id or alias         (e.g. !abc:example.org)
  MATRIX_TOKEN   user access‑token

ENV (optional)
  LISTEN_MODE = all | tail | once   (default: all)
  TAIL_N      = N messages for tail (default: 20000)
  TIMEOUT     = seconds for mode=all (default: 20)
"""

import os, sys, json, subprocess, shlex, hashlib, colorsys, logging, pathlib
import html as htmllib
from datetime import datetime, timezone
import collections

# ────────────────────────────── configuration ──────────────────────────
HS, USER, ROOM, TOKEN = (os.environ[k] for k in
                         ("MATRIX_HS", "MATRIX_USER", "MATRIX_ROOM", "MATRIX_TOKEN"))
MODE     = os.getenv("LISTEN_MODE", "all").lower()
TAIL_N   = os.getenv("TAIL_N", "20000")
TIMEOUT  = int(os.getenv("TIMEOUT", "20"))

logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s: %(message)s",
                    stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"              # silence nio crypto debug

# ────────────────────────────── credentials ────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)

if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver":   HS,
        "user_id":      USER,
        "access_token": TOKEN,
        "device_id":    "GH",
        "room_id":      ROOM,
        "default_room": ROOM,
    }))
CRED = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ────────────────────────────── helpers ────────────────────────────────
def pastel(uid: str) -> str:
    h = int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16) / 0xffffffff
    r, g, b = colorsys.hls_to_rgb(h, .72, .50)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev):       # dt (UTC)
    return datetime.fromtimestamp(ev["origin_server_ts"]/1000, tz=timezone.utc)

def json_lines(raw: str):
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln and ln[0] in "{[":
            try: yield json.loads(ln)
            except json.JSONDecodeError:
                logging.debug(f"skip ≠json → {ln[:70]}")

def mc(cmd, timeout=None) -> str:
    logging.debug("⟹ " + " ".join(map(shlex.quote, cmd)))
    try:
        res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logging.warning(f"matrix‑commander timed out after {timeout}s – using partial output")
        res = exc
    for l in res.stderr.splitlines(): logging.debug(l)
    return res.stdout

# ────────────────────────────── ensure membership ───────────────────────
try:   mc(["matrix-commander", *CRED, "--room-join", ROOM])
except subprocess.CalledProcessError: pass        # already joined / public

# ────────────────────────────── room meta ───────────────────────────────
pretty, topic = ROOM, ""
try:
    meta = next(json_lines(mc(
        ["matrix-commander", *CRED,
         "--room", ROOM, "--get-room-info", "--output", "json"])), {})
    pretty = (meta.get("display_name") or meta.get("room_name") or
              meta.get("room_canonical_alias") or meta.get("room_alias") or ROOM)
    topic  = meta.get("topic") or ""
except Exception as e:
    logging.warning(f"meta fetch failed: {e}")

# ────────────────────────────── fetch events ────────────────────────────
listen = {
    "all" : ["--listen","all","--listen-self"],
    "tail": ["--listen","tail","--tail",TAIL_N,"--listen-self"],
    "once": ["--listen","once","--listen-self"],
}[MODE]

raw = mc(["matrix-commander", *CRED, "--room", ROOM,
          *listen, "--output", "json"],
         timeout=TIMEOUT if MODE=="all" else None)

events = [(e["source"] if "source" in e else e)
          for e in json_lines(raw)
          if (e.get("type") == "m.room.message"
              or e.get("source", {}).get("type") == "m.room.message")]

logging.info(f"{len(events)} message events")
if not events:
    logging.error("no events – nothing to archive"); sys.exit(1)

# ────────────────────────────── thread map (1‑level) ────────────────────
roots   : dict[str,dict]            = {}
replies : collections.defaultdict[list[str]] = collections.defaultdict(list)

for ev in events:
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread" and rel.get("event_id") != ev["event_id"]:
        replies[rel["event_id"]].append(ev["event_id"])
    else:
        roots[ev["event_id"]] = ev

ordered_roots = sorted(roots.values(), key=when)

# ────────────────────────────── build outputs ───────────────────────────
now_txt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

plain = [f"# {pretty}"]
if topic: plain += [f"# {t}" for t in topic.splitlines()]
plain += [f"# exported: {now_txt}", ""]

page  = [
    "<!doctype html><meta charset=utf-8>",
    f"<title>{htmllib.escape(pretty)} – archive</title>",
    "<style>",
    "body{background:#111;color:#eee;font:15px/1.55 ui-monospace,monospace;"
    "padding:1.2em clamp(1.2em,5vw,3em)}",
    ".hdr{font:22px/1.35 ui-monospace;margin-bottom:.4em}",
    ".topic{color:#aaa;margin:.3em 0 1.2em}",
    ".msg{display:flex;gap:.6em;margin:.28em 0}",
    ".time{color:#777;flex:none;width:3.5ch;text-align:right;margin-right:.4em}",
    ".user{font-weight:600}",
    ".thread{margin-left:2em;border-left:2px solid #444;padding-left:1em}",
    "a{color:#9cf;text-decoration:none}",
    "</style>",
    f"<div class=hdr>{htmllib.escape(pretty)}</div>",
]
if topic:
    page.append(f"<div class=topic>{htmllib.escape(topic)}</div>")
page += [
    "<p><a href='room_log.txt'>⇩ download plain‑text</a></p>",
    "<hr>",
]

def emit(ev, depth):
    t   = when(ev).strftime("%H:%M")          # ← compact timestamp
    usr = ev["sender"]
    body= ev["content"].get("body","")
    # text
    plain.append(f"{'  '*depth}{when(ev).isoformat()} {usr}: {body}")
    # html
    wrapper_open  = "<div class='thread'>" if depth else ""
    wrapper_close = "</div>"                 if depth else ""
    page.append(
        f"{wrapper_open}"
        f"<div class='msg'><span class=time>{t}&nbsp;</span>"
        f"<span class='user' style='color:{pastel(usr)}'>{htmllib.escape(usr)}</span>"
        f"<span>{htmllib.escape(body)}</span></div>"
        f"{wrapper_close}"
    )

for root in ordered_roots:
    emit(root, 0)
    for cid in sorted(replies.get(root["event_id"], []),
                      key=lambda i: when(next(e for e in events if e["event_id"]==i))):
        emit(next(e for e in events if e["event_id"]==cid), 1)

# ────────────────────────────── write files ─────────────────────────────
pathlib.Path("room_log.txt").write_text("\n".join(plain)+"\n",  encoding="utf-8")
pathlib.Path("index.html" ).write_text("\n".join(page )+"\n",  encoding="utf-8")
logging.info("archive written  →  index.html  &  room_log.txt  ✓")

