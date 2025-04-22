#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive a public, un‑encrypted Matrix room to:
    • index.html   (pretty, threaded)
    • room_log.txt (plain‑text)

ENV vars (required)  :  MATRIX_HS  MATRIX_USER  MATRIX_ROOM  MATRIX_TOKEN
optional:
    LISTEN_MODE  = all | tail | once        (default: all)
    TAIL_N       = N for --tail             (default: 20000)
    TIMEOUT      = seconds for mode=all     (default: 20)
    TZ           = IANA zone for timestamps (default: UTC)
"""

import os, sys, json, subprocess, shlex, logging, hashlib, colorsys, html
import collections, pathlib, zoneinfo
from datetime import datetime, timezone

# ────── logging ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s: %(message)s",
                    stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"          # silence nio’s crypto spam

# ────── ENV ─────────────────────────────────────────────────────────────
HS, USER, ROOM, TOKEN = (os.environ[k] for k in
                         ("MATRIX_HS", "MATRIX_USER", "MATRIX_ROOM", "MATRIX_TOKEN"))
MODE     = os.getenv("LISTEN_MODE", "all").lower()          # all|tail|once
TAIL_N   = os.getenv("TAIL_N", "20000")
TIMEOUT  = int(os.getenv("TIMEOUT", "20"))
LOCAL_TZ = zoneinfo.ZoneInfo(os.getenv("TZ")) if "TZ" in os.environ else timezone.utc
FMT_TS   = "%Y-%m-%d %H:%M:%S %Z"        # keep TS together with narrow NB‑spaces

# ────── credentials & store ────────────────────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store")
store_dir.mkdir(exist_ok=True)

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

# ────── helpers ─────────────────────────────────────────────────────────
def pastel(uid: str) -> str:
    h = int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16) / 0xffffffff
    r, g, b = colorsys.hls_to_rgb(h, .68, .46)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev):                # → aware datetime
    return datetime.fromtimestamp(ev["origin_server_ts"] / 1000, tz=LOCAL_TZ)

def json_lines(raw: str):
    """yield JSON objects found line‑by‑line (skip human chatter)"""
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln and ln[0] in "{[":
            try:
                yield json.loads(ln)
            except json.JSONDecodeError:
                logging.debug("skip ≠json → "+ln[:80])

def run_mc(cmd: list[str], *, timeout: int | None = None) -> str:
    logging.debug("⟹ " + " ".join(map(shlex.quote, cmd)))
    try:
        res = subprocess.run(cmd, text=True, capture_output=True,
                             timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        res = e
        logging.warning(f"matrix‑commander timed‑out after {timeout}s; keeping partial output")
    for l in res.stderr.splitlines():
        logging.debug(l)
    return res.stdout

# ────── make sure bot is in the room ───────────────────────────────────
try: run_mc(["matrix-commander", *CRED, "--room-join", ROOM])
except subprocess.CalledProcessError: pass      # already joined / not allowed

# ────── room meta (pretty title / topic) ───────────────────────────────
pretty, topic = ROOM, ""
try:
    meta = next(json_lines(run_mc(
        ["matrix-commander", *CRED, "--room", ROOM,
         "--get-room-info", "--output", "json"])), {})
    pretty = (meta.get("display_name") or meta.get("room_name") or
              meta.get("room_canonical_alias") or meta.get("room_alias") or ROOM)
    topic  = meta.get("topic") or ""
except Exception as e:
    logging.warning(f"room‑info fetch failed: {e}")

# ────── collect events ─────────────────────────────────────────────────
listen_args = {
    "all":  ["--listen", "all",  "--listen-self"],
    "tail": ["--listen", "tail", "--tail", TAIL_N, "--listen-self"],
    "once": ["--listen", "once", "--listen-self"],
}[MODE]
raw = run_mc(["matrix-commander", *CRED, "--room", ROOM,
              *listen_args, "--output", "json"],
             timeout=TIMEOUT if MODE == "all" else None)

# unwrap “source” wrapper that matrix‑commander adds around events
events = [(e["source"] if "source" in e else e)
          for e in json_lines(raw)
          if (e.get("type") == "m.room.message" or
              e.get("source", {}).get("type") == "m.room.message")]

logging.info(f"{len(events)} message events")
if not events:
    logging.error("no events → abort")
    sys.exit(1)

# ────── build thread map ───────────────────────────────────────────────
by_id       : dict[str, dict]            = {}
children    : collections.defaultdict[list[str]] = collections.defaultdict(list)
thread_root : set[str]                   = set()

for ev in events:
    by_id[ev["event_id"]] = ev
# detect replies / roots
for ev in events:
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        root = rel.get("event_id")
        if root and root != ev["event_id"]:          # reply → add edge
            children[root].append(ev["event_id"])
        thread_root.add(root)                        # even if fallback

# messages that are **not** replies (true roots)
roots = sorted({e for e in events if e["event_id"] not in children or
                e["event_id"] in thread_root}, key=when)

# ────── assemble plain‑text & HTML ─────────────────────────────────────
stamp = datetime.now(LOCAL_TZ).strftime(FMT_TS)

txt_lines = [
    f"# {pretty}",
    *(f"# {line}" for line in topic.splitlines()),
    f"# exported: {stamp}",
    ""
]

html_lines = [
    "<!doctype html><meta charset=utf-8>",
    f"<title>{html.escape(pretty)} – archive</title>",
    "<style>",
    "body{background:#111;color:#eee;font:15px/1.5 ui-monospace,monospace;"
    "padding:1.2em max(1.2em,5vw)}",
    ".hdr{font:22px/1.35 ui-monospace;margin-bottom:.4em}",
    ".topic{color:#aaa;margin:.3em 0 1.2em}",
    ".download{color:#9cf;text-decoration:none}",
    ".msg{margin:.25em 0}",
    ".lvl0{border-left:3px solid #555;padding-left:1em}",
    ".lvl1{border-left:3px solid #666;padding-left:1em}",
    ".lvl2{border-left:3px solid #777;padding-left:1em}",
    ".lvl3{border-left:3px solid #888;padding-left:1em}",
    "time{color:#888;margin-right:.6em}",
    ".u{font-weight:600}",
    "</style>",
    f"<div class=hdr>{html.escape(pretty)}</div>",
]
if topic:
    html_lines.append(f"<div class=topic>{html.escape(topic)}</div>")
html_lines += [
    "<p><a class=download href='room_log.txt'>⇩ download plain‑text</a></p>",
    "<hr>"
]

def emit(ev, lvl: int):
    ts = when(ev).strftime(FMT_TS)
    sender = ev["sender"]
    body   = ev["content"].get("body", "")
    indent = " " * (4 * lvl)
    txt_lines.append(f"{indent}{ts} {sender}: {body}")

    cls = f"msg lvl{min(lvl,3)}"
    html_lines.append(
        f"<div class='{cls}'>"
        f"<time>{ts}</time>"
        f"<span class='u' style='color:{pastel(sender)}'>{html.escape(sender)}</span>: "
        f"{html.escape(body)}</div>"
    )

def walk(ev, lvl=0):
    emit(ev, lvl)
    for cid in sorted(children.get(ev["event_id"], []), key=lambda eid: when(by_id[eid])):
        walk(by_id[cid], lvl+1)

for root in roots:
    walk(root)

# ────── write files ────────────────────────────────────────────────────
pathlib.Path("room_log.txt").write_text("\n".join(txt_lines) + "\n",  encoding="utf-8")
pathlib.Path("index.html" ).write_text("\n".join(html_lines) + "\n", encoding="utf-8")

logging.info("✓ archive written →  index.html  &  room_log.txt")

