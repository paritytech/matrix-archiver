#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive a public, un‑encrypted Matrix room into index.html + room_log.txt
"""

import os, sys, json, subprocess, shlex, hashlib, colorsys, html, logging
import collections, pathlib
from datetime import datetime, timezone

# ───── env ──────────────────────────────────────────────────────────────
HS      = os.environ["MATRIX_HS"]
USER_ID = os.environ["MATRIX_USER"]
ROOM    = os.environ["MATRIX_ROOM"]
TOKEN   = os.environ["MATRIX_TOKEN"]

LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()      # all|tail|once
TAIL_N      = os.getenv("TAIL_N", "20000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", 20))

# ───── logging ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s: %(message)s", stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"

# ───── creds ───────────────────────────────────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)

if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver": HS, "user_id": USER_ID, "access_token": TOKEN,
        "device_id": "GH", "room_id": ROOM, "default_room": ROOM,
    }))
CRED = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ───── helpers ─────────────────────────────────────────────────────────
def pastel(uid: str) -> str:
    h = int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(h, .70, .45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev): return datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)
nice_user = lambda u: u.lstrip("@").split(":",1)[0]

def json_lines(raw:str):
    out=[]
    for ln in raw.splitlines():
        ln=ln.strip()
        if ln and ln[0] in "{[":
            try: out.append(json.loads(ln))
            except json.JSONDecodeError: pass
    return out

def run(cmd, timeout=None):
    logging.debug("⟹ %s", " ".join(map(shlex.quote, cmd)))
    res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    for l in res.stderr.splitlines(): logging.debug(l)
    if res.returncode not in (0, None): raise subprocess.CalledProcessError(res.returncode, cmd)
    return res.stdout

# ───── join room (idempotent) ──────────────────────────────────────────
try: run(["matrix-commander", *CRED, "--room-join", ROOM])
except subprocess.CalledProcessError: pass

# ───── human room title ────────────────────────────────────────────────
room_title = ROOM
try:
    meta = json_lines(run(["matrix-commander", *CRED,
                           "--room", ROOM, "--get-room-info", "--output", "json"]))
    if meta:
        for k in ("display_name","name","room_display_name","room_name",
                  "room_canonical_alias","room_alias"):
            if meta[0].get(k):
                room_title = meta[0][k]; break
except Exception as e:
    logging.warning("meta fetch failed: %s", e)

# ───── fetch events ────────────────────────────────────────────────────
listen = {"all":["--listen","all","--listen-self"],
          "tail":["--listen","tail","--tail",TAIL_N,"--listen-self"],
          "once":["--listen","once","--listen-self"]}[LISTEN_MODE]

raw = run(["matrix-commander", *CRED, "--room", ROOM, *listen, "--output","json"],
          timeout=TIMEOUT_S if LISTEN_MODE=="all" else None)

events = []
for j in json_lines(raw):
    ev = j.get("source", j)
    if ev.get("type")=="m.room.message": events.append(ev)

logging.info("%d message events", len(events))
if not events: sys.exit("no events")

# ───── flat threading ─────────────────────────────────────────────────
by_id={e["event_id"]:e for e in events}
threads=collections.defaultdict(list)
for e in events:
    rel=e["content"].get("m.relates_to",{})
    if rel.get("rel_type")=="m.thread":
        threads[rel["event_id"]].append(e["event_id"])

roots = sorted([e for e in events if e["event_id"] not in
                {c for kids in threads.values() for c in kids}], key=when)

# ───── plaintext ───────────────────────────────────────────────────────
stamp=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
txt=[f"# room: {room_title}",f"# exported: {stamp}"]

def add_text(ev,lvl):
    ts=when(ev).strftime("%Y-%m-%d %H:%M")
    arrow="↳ " if lvl else ""
    txt.append(f"{'  '*lvl}{arrow}{ts} {nice_user(ev['sender'])}: {ev['content'].get('body','')}")

for r in roots:
    add_text(r,0)
    for cid in sorted(threads[r["event_id"]], key=lambda i: when(by_id[i])):
        add_text(by_id[cid],1)

# ───── HTML ────────────────────────────────────────────────────────────
html_lines=[
    "<!doctype html><meta charset=utf-8>",
    f"<title>{html.escape(room_title)} – archive</title>",
    "<style>",
    "body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#eee;padding:1em}",
    ".msg{white-space:pre-wrap}",
    "time{color:#888;margin-right:.5em}",
    ".u{font-weight:600}",
    ".reply{margin-left:2ch}",
    "a{color:#9cf;text-decoration:none}",
    "</style>",
    f"<h1>{html.escape(room_title)}</h1>",
    "<p><a href='room_log.txt'>⇩ download plaintext</a></p>",
    "<hr>",
]

def add_html(ev,lvl):
    ts=when(ev).strftime("%Y‑%m‑%d %H:%M")
    usr=nice_user(ev["sender"])
    body=html.escape(ev["content"].get("body",""))
    cls="msg reply" if lvl else "msg"
    html_lines.append(
        f"<div class='{cls}'>"
        f"<time>{ts}</time>&ensp;"
        f"<span class='u' style='color:{pastel(ev['sender'])}'>{usr}</span>: "
        f"{body}</div>"
    )

for r in roots:
    add_html(r,0)
    for cid in sorted(threads[r["event_id"]], key=lambda i: when(by_id[i])):
        add_html(by_id[cid],1)

# ───── write files ─────────────────────────────────────────────────────
pathlib.Path("room_log.txt").write_text("\n".join(txt)+"\n", encoding="utf-8")
pathlib.Path("index.html").write_text("\n".join(html_lines)+"\n", encoding="utf-8")
logging.info("archive written ✓")

