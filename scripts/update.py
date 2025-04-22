#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive a public, un‑encrypted Matrix room into:
    •  index.html   (pretty, threaded)
    •  room_log.txt (plain‑text)
Designed for CI / GitHub Actions.

ENV:
  MATRIX_HS       e.g. https://matrix.org
  MATRIX_USER     @bot:matrix.org
  MATRIX_ROOM     !roomid:matrix.org  (or full alias)
  MATRIX_TOKEN    <access‑token>
  LISTEN_MODE     all|tail|once   (default all)
  TAIL_N          (for mode=tail) default 20000
  TIMEOUT         seconds for mode=all, default 20
"""
import os, sys, json, subprocess, shlex, hashlib, colorsys, html, logging
import collections, pathlib, zoneinfo
from datetime import datetime, timezone

# ───── basic logging ────────────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s: %(message)s",
                    stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"                     # silence nio

# ───── env vars ────────────────────────────────────────────────────────
HS, USER, ROOM, TOKEN = (os.environ[k] for k in
                         ("MATRIX_HS","MATRIX_USER","MATRIX_ROOM","MATRIX_TOKEN"))
LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()     # all|tail|once
TAIL_N      = os.getenv("TAIL_N", "20000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", "20"))
LOCAL_TZ    = zoneinfo.ZoneInfo(os.getenv("TZ")) if "TZ" in os.environ else timezone.utc

logging.debug(f"room={ROOM} mode={LISTEN_MODE}")

# ───── credentials & store ─────────────────────────────────────────────
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

# ───── helpers ─────────────────────────────────────────────────────────
def pastel(uid:str) -> str:
    h = int(hashlib.sha1(uid.encode()).hexdigest()[:8],16)/0xffffffff
    r,g,b = colorsys.hls_to_rgb(h, .68, .46)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev):
    return datetime.fromtimestamp(ev["origin_server_ts"]/1000, tz=LOCAL_TZ)

FMT_TS = "%Y-%m-%d %H:%M:%S %Z"        # narrow no‑break spaces keep it together

def json_lines(raw:str):
    for ln in raw.splitlines():
        ln=ln.strip()
        if ln and ln[0] in "{[":
            try: yield json.loads(ln)
            except json.JSONDecodeError: logging.debug("skip → "+ln[:60])

def run_mc(cmd:list, *, timeout:int|None=None) -> str:
    logging.debug("⟹ "+ " ".join(map(shlex.quote,cmd)))
    try:
        res = subprocess.run(cmd, text=True, capture_output=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        res = exc
        logging.warning(f"timeout after {timeout}s, using whatever was printed")

    for l in res.stderr.splitlines(): logging.debug(l)
    return res.stdout

# ───── ensure joined ───────────────────────────────────────────────────
try: run_mc(["matrix-commander",*CRED,"--room-join",ROOM])
except subprocess.CalledProcessError: pass

# ───── room metadata ───────────────────────────────────────────────────
pretty = ROOM ; topic = ""
try:
    meta = next(json_lines(run_mc(
        ["matrix-commander",*CRED,"--room",ROOM,
         "--get-room-info","--output","json"])),{})
    pretty = meta.get("room_name") or meta.get("room_canonical_alias") \
             or meta.get("room_alias") or ROOM
    topic  = meta.get("topic") or ""
except Exception as e:
    logging.warning(f"room‑info failed: {e}")

# ───── fetch events ────────────────────────────────────────────────────
listen_args = {
    "all" : ["--listen","all","--listen-self"],
    "tail": ["--listen","tail","--tail",TAIL_N,"--listen-self"],
    "once": ["--listen","once","--listen-self"],
}[LISTEN_MODE]
raw = run_mc(["matrix-commander",*CRED,"--room",ROOM,*listen_args,
              "--output","json"],
             timeout=TIMEOUT_S if LISTEN_MODE=="all" else None)

# unwrap “source”
events = [(e["source"] if "source" in e else e)
          for e in json_lines(raw)
          if (e.get("type")=="m.room.message" or
              e.get("source",{}).get("type")=="m.room.message")]

logging.info(f"{len(events)} message events parsed")
if not events:
    logging.error("no events – abort"); sys.exit(1)

# ───── threading ───────────────────────────────────────────────────────
by_id, kids = {}, collections.defaultdict(list)
for ev in events:
    by_id[ev["event_id"]]=ev
    rel = ev["content"].get("m.relates_to",{})
    if rel.get("rel_type")=="m.thread":
        kids[rel["event_id"]].append(ev["event_id"])
roots = sorted(
    [e for e in events if e["event_id"] not in
        {c for k in kids.values() for c in k}],
    key=when)

# ───── assemble logs ───────────────────────────────────────────────────
stamp = datetime.now(LOCAL_TZ).strftime(FMT_TS)
txt  = [f"# {pretty}", *(f"# {line}" for line in (topic.splitlines() or [])),
        f"# exported: {stamp}", ""]
html_lines = [
    "<!doctype html><meta charset=utf-8>",
    f"<title>{html.escape(pretty)} – archive</title>",
    "<style>",
    "body{background:#111;color:#eee;font:15px/1.45 ui-monospace,monospace;"
    "padding:1.2em max(1.2em,5vw)}",
    ".hdr{font:20px/1.3 ui-monospace;margin-bottom:.4em}",
    ".topic{color:#aaa;margin:.2em 0 1em}",
    ".log{border-left:3px solid #444;padding-left:1em}",
    ".msg{margin:.25em 0}",
    ".lvl1{border-color:#555}",
    ".lvl2{border-color:#666}",
    ".lvl3{border-color:#777}",
    "time{color:#888;margin-right:.6em}",
    ".u{font-weight:600}",
    ".download{color:#9cf;text-decoration:none}",
    "</style>",
    f"<div class=hdr>{html.escape(pretty)}</div>",
]
if topic:
    html_lines.append(f"<div class=topic>{html.escape(topic)}</div>")
html_lines.append("<p><a class=download href='room_log.txt'>"
                  "⇩ download plain‑text</a></p><hr>")
html_lines.append("<div class='log lvl0'>")

def emit(ev, lvl:int):
    ts = when(ev).strftime(FMT_TS)
    sender = ev["sender"]; body = ev["content"].get("body","")
    indent = " " * 4*lvl
    txt.append(f"{indent}{ts} {sender}: {body}")
    cls = f"msg lvl{lvl}"
    html_lines.append(
        f"<div class='{cls}'>"
        f"<time>{ts}</time>"
        f"<span class='u' style='color:{pastel(sender)}'>{html.escape(sender)}</span>: "
        f"{html.escape(body)}</div>")

def walk(ev,lvl=0):
    emit(ev,lvl)
    for cid in sorted(kids[ev["event_id"]], key=lambda i: when(by_id[i])):
        walk(by_id[cid], lvl+1)

for root in roots: walk(root,0)
html_lines.append("</div>")

# ───── write files ─────────────────────────────────────────────────────
pathlib.Path("room_log.txt").write_text("\n".join(txt)+"\n",  encoding="utf-8")
pathlib.Path("index.html" ).write_text("\n".join(html_lines)+"\n",encoding="utf-8")

logging.info("✓ archive written – index.html & room_log.txt")

