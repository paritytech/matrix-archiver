#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive a public, un‑encrypted Matrix room into
    • index.html (pretty, threaded)
    • room_log.txt (plain text)

ENV:  MATRIX_HS  MATRIX_USER  MATRIX_ROOM  MATRIX_TOKEN
OPT:  LISTEN_MODE=all|tail|once  TAIL_N  TIMEOUT
"""

import os, sys, json, subprocess, shlex, hashlib, colorsys, logging
import collections, pathlib
import html as htmllib                         # <── keep original module here
from datetime import datetime, timezone

# ─────────────── ENV ────────────────────────────────────────────────────
HS, USER, ROOM, TOKEN = (os.environ[k] for k in
                         ("MATRIX_HS", "MATRIX_USER", "MATRIX_ROOM", "MATRIX_TOKEN"))
MODE     = os.getenv("LISTEN_MODE", "all").lower()           # all|tail|once
TAIL_N   = os.getenv("TAIL_N", "20000")
TIMEOUT  = int(os.getenv("TIMEOUT", "20"))                   # when MODE == all

# ─────────────── LOGGING ───────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s: %(message)s", stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"         # mute nio crypto spew
logging.debug(f"homeserver={HS}  user={USER}  room={ROOM}")

# ─────────────── CREDENTIALS ───────────────────────────────────────────
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

# ─────────────── HELPERS ───────────────────────────────────────────────
def pastel(uid: str) -> str:
    h = int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16) / 0xffffffff
    r, g, b = colorsys.hls_to_rgb(h, .70, .45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev):
    return datetime.fromtimestamp(ev["origin_server_ts"]/1000.0, tz=timezone.utc)

def json_lines(raw: str):
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln and ln[0] in "{[":
            try: yield json.loads(ln)
            except json.JSONDecodeError:
                logging.debug(f"skip ≠json → {ln[:70]}")

def run_mc(cmd, timeout=None) -> str:
    logging.debug("⟹ " + " ".join(map(shlex.quote, cmd)))
    try:
        res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        logging.warning(f"matrix‑commander timed out after {timeout}s – using partial output")
        res = exc
    for l in res.stderr.splitlines(): logging.debug(l)
    return res.stdout

# ─────────────── JOIN ─────────────────────────────────────────────────
try:   run_mc(["matrix-commander", *CRED, "--room-join", ROOM])
except subprocess.CalledProcessError: pass     # already joined / public

# ─────────────── ROOM META ─────────────────────────────────────────────
pretty, topic = ROOM, ""
try:
    meta = next(json_lines(run_mc(
        ["matrix-commander", *CRED,
         "--room", ROOM, "--get-room-info", "--output", "json"])), {})
    pretty = (meta.get("display_name") or meta.get("room_name") or
              meta.get("room_canonical_alias") or meta.get("room_alias") or ROOM)
    topic  = meta.get("topic") or ""
except Exception as e:
    logging.warning(f"meta fetch failed: {e}")

# ─────────────── FETCH EVENTS ─────────────────────────────────────────
listen_args = {
    "all" : ["--listen","all","--listen-self"],
    "tail": ["--listen","tail","--tail",TAIL_N,"--listen-self"],
    "once": ["--listen","once","--listen-self"],
}[MODE]

raw = run_mc(["matrix-commander", *CRED, "--room", ROOM,
              *listen_args, "--output", "json"],
             timeout=TIMEOUT if MODE=="all" else None)

events = [(e["source"] if "source" in e else e)
          for e in json_lines(raw)
          if (e.get("type") == "m.room.message"
              or e.get("source", {}).get("type") == "m.room.message")]

logging.info(f"{len(events)} message events")
if not events:
    logging.error("no events – nothing to archive"); sys.exit(1)

# ─────────────── THREAD MAP (single level) ────────────────────────────
roots   : dict[str,dict]            = {}
replies : collections.defaultdict[list[str]] = collections.defaultdict(list)

for ev in events:
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread" and rel.get("event_id") != ev["event_id"]:
        replies[rel["event_id"]].append(ev["event_id"])
    else:
        roots[ev["event_id"]] = ev

ordered_roots = sorted(roots.values(), key=when)

# ─────────────── BUILD OUTPUTS ────────────────────────────────────────
ts_now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

txt  = [f"# {pretty}"]
if topic: txt += [f"# {t}" for t in topic.splitlines()]
txt += [f"# exported: {ts_now}", ""]

page = [                                               # ← renamed from `html`
    "<!doctype html><meta charset=utf-8>",
    f"<title>{htmllib.escape(pretty)} – archive</title>",
    "<style>",
    "body{background:#111;color:#eee;font:15px/1.5 ui-monospace,monospace;"
    "padding:1.2em max(1.2em,5vw)}",
    ".hdr{font:22px/1.35 ui-monospace;margin-bottom:.4em}",
    ".topic{color:#aaa;margin:.3em 0 1.2em}",
    ".root{margin:.35em 0 .15em;padding-left:.8em;border-left:3px solid #555}",
    ".reply{margin:.25em 0 .15em .8em;padding-left:.8em;border-left:3px solid #666}",
    "time{color:#888;margin-right:.6em}",
    ".u{font-weight:600}",
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
    ts  = when(ev).strftime("%Y-%m-%d %H:%M:%S UTC")
    usr = ev["sender"]
    bod = ev["content"].get("body","")
    # text
    txt.append(f"{'  '*depth}{ts} {usr}: {bod}")
    # html
    cls = "root" if depth == 0 else "reply"
    page.append(
        f"<div class='{cls}'>"
        f"<time>{ts}</time>"
        f"<span class='u' style='color:{pastel(usr)}'>{htmllib.escape(usr)}</span>: "
        f"{htmllib.escape(bod)}</div>"
    )

for root in ordered_roots:
    emit(root, 0)
    for cid in sorted(replies.get(root["event_id"], []),
                      key=lambda x: when(next(e for e in events if e["event_id"]==x))):
        emit(next(e for e in events if e["event_id"]==cid), 1)

# ─────────────── WRITE FILES ───────────────────────────────────────────
pathlib.Path("room_log.txt").write_text("\n".join(txt)+"\n",  encoding="utf-8")
pathlib.Path("index.html" ).write_text("\n".join(page)+"\n", encoding="utf-8")
logging.info("archive written  →  index.html  &  room_log.txt  ✓")

