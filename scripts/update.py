#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive a public, un‑encrypted Matrix room into index.html + room_log.txt
Designed for CI / GitHub‑Actions.

• LISTEN_MODE  –  all | tail | once      (default: all)
• TAIL_N       –  how many messages for mode = tail   (default: 20 000)
• TIMEOUT      –  seconds before we kill a long “all” sync  (default: 20)

Required env:
    MATRIX_HS      homeserver   (https://matrix.org or https://…)
    MATRIX_USER    full user id (@alice:example.org)
    MATRIX_ROOM    room id (!abc:example.org)  or canonical alias
    MATRIX_TOKEN   access token of that account
"""

import os, sys, json, subprocess, shlex, hashlib, colorsys, html, logging
import collections, pathlib
from datetime import datetime, timezone

# ─────────────────────────── configuration ──────────────────────────────
HS        = os.environ["MATRIX_HS"]
USER_ID   = os.environ["MATRIX_USER"]
ROOM      = os.environ["MATRIX_ROOM"]
TOKEN     = os.environ["MATRIX_TOKEN"]

LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()          # all|tail|once
TAIL_N      = os.getenv("TAIL_N", "20000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", 20))

# ───────────────────────────── logging ──────────────────────────────────
logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s: %(message)s", stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"           # silence nio crypto chatter
logging.debug(f"homeserver={HS}   room={ROOM}")

# ───────────────────── credentials & persistent store ───────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store")
store_dir.mkdir(exist_ok=True)

if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver"  : HS,
        "user_id"     : USER_ID,
        "access_token": TOKEN,
        "device_id"   : "GH",
        "room_id"     : ROOM,
        "default_room": ROOM,
    }))
CRED_OPTS = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ───────────────────────── helper functions ─────────────────────────────
def pastel(uid: str) -> str:
    """Deterministic pleasant color from user id."""
    hue = int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(hue, .70, .45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev):                           # → datetime (UTC)
    return datetime.utcfromtimestamp(ev["origin_server_ts"] / 1000.0)

def nice_user(u: str) -> str:           # "@alice:example.org" →  "alice"
    if u.startswith("@"):
        u = u[1:]
    return u.split(":", 1)[0]

def json_lines(raw: str):
    """Parse line‑delimited JSON, skipping non‑JSON noise."""
    objs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            objs.append(json.loads(line))
        except json.JSONDecodeError:
            logging.debug("skipped non‑json line: %s", line[:80])
    return objs

def _stream(lines):
    for ln in lines:
        logging.debug(ln.rstrip())
        yield ln

def run_stream(cmd):
    """Run a matrix‑commander call, stream stderr→DEBUG, return stdout str."""
    logging.debug("⟹ %s", " ".join(map(shlex.quote, cmd)))
    proc = subprocess.Popen(cmd, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in _stream(proc.stderr):       # drain stderr
        pass
    out = "".join(_stream(proc.stdout))
    if proc.wait():
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return out

def run_timeout(cmd, secs):
    """Run with timeout, killing the process after *secs*; return stdout str."""
    logging.debug("⟹ %s  (timeout=%ss)", " ".join(map(shlex.quote, cmd)), secs)
    try:
        res = subprocess.run(cmd, text=True, capture_output=True, timeout=secs)
    except subprocess.TimeoutExpired as e:
        logging.warning("timeout: killed after %ss", secs)
        res = e               # still has .stdout / .stderr
    for ln in res.stderr.splitlines(): logging.debug(ln)
    for ln in res.stdout.splitlines(): logging.debug(ln)
    return res.stdout

# ───────────────── ensure we’re in the room (idempotent) ────────────────
try:
    run_stream(["matrix-commander", *CRED_OPTS, "--room-join", ROOM])
except subprocess.CalledProcessError:
    pass

# ───────────────── prettify room title ──────────────────────────────────
room_title = ROOM
try:
    meta = json_lines(run_stream(
        ["matrix-commander", *CRED_OPTS,
         "--room", ROOM, "--get-room-info", "--output", "json"]))
    if meta:
        for key in ("room_display_name", "room_name",
                    "room_canonical_alias", "room_alias"):
            if meta[0].get(key):
                room_title = meta[0][key]
                break
except Exception as e:
    logging.warning("room‑meta fetch failed: %s", e)

# ───────────────────── fetch timeline messages ──────────────────────────
listen_args = {
    "all" : ["--listen", "all",  "--listen-self"],
    "tail": ["--listen", "tail", "--tail", TAIL_N, "--listen-self"],
    "once": ["--listen", "once", "--listen-self"],
}[LISTEN_MODE]

cmd = ["matrix-commander", *CRED_OPTS,
       "--room", ROOM, *listen_args, "--output", "json"]

raw = (run_timeout(cmd, TIMEOUT_S) if LISTEN_MODE == "all"
       else run_stream(cmd))

# unwrap “source” wrapper (matrix‑commander ≥ v8)
events = []
for j in json_lines(raw):
    obj = j.get("source", j)        # prefer inner “source” if present
    if obj.get("type") == "m.room.message":
        events.append(obj)

logging.info("%d message events", len(events))
if not events:
    logging.error("no events – nothing to archive")
    sys.exit(1)

# ───────────────────────── thread bookkeeping ───────────────────────────
by_id   = {ev["event_id"]: ev for ev in events}
threads = collections.defaultdict(list)      # parent id → [child ids]

for ev in events:
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        threads[rel["event_id"]].append(ev["event_id"])

roots = sorted(
    [ev for ev in events
     if ev["event_id"] not in {c for kids in threads.values() for c in kids}],
    key=when)

# ───────────────────────── build TEXT output ────────────────────────────
stamp   = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
txt_out = [f"# room: {room_title}", f"# exported: {stamp}"]

def emit_text(ev, lvl: int):
    ts = when(ev).strftime("%Y‑%m‑%d %H:%M")      # compact UTC timestamp
    arrow = "↳ " if lvl else ""
    body  = ev["content"].get("body", "")
    txt_out.append(f"{'  '*lvl}{arrow}{ts} {nice_user(ev['sender'])}: {body}")

for root in roots:
    emit_text(root, 0)
    for cid in sorted(threads[root["event_id"]], key=lambda i: when(by_id[i])):
        emit_text(by_id[cid], 1)

# ───────────────────────── build HTML output ────────────────────────────
html_out = [
    "<!doctype html>",
    "<meta charset=utf-8>",
    f"<title>{html.escape(room_title)} – archive</title>",
    "<style>",
    "body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#eee;padding:1em}"
    "time{color:#888;margin-right:.5em}"
    ".u{font-weight:600}"
    ".reply{margin-left:2ch}"
    "a{color:#9cf;text-decoration:none}",
    "</style>",
    f"<h1>{html.escape(room_title)}</h1>",
    "<p><a href='room_log.txt'>⇩ download plaintext</a></p>",
    "<hr><pre>",
]

def emit_html(ev, lvl: int):
    ts  = when(ev).strftime("%Y‑%m‑%d&nbsp;%H:%M")
    usr = nice_user(ev["sender"])
    body = html.escape(ev["content"].get("body", ""))
    cls  = "reply" if lvl else ""
    html_out.append(
        f"<div class='{cls}'>"
        f"<time>{ts}</time> "
        f"<span class='u' style='color:{pastel(ev['sender'])}'>{usr}</span>: "
        f"{body}</div>"
    )

for root in roots:
    emit_html(root, 0)
    for cid in sorted(threads[root["event_id"]], key=lambda i: when(by_id[i])):
        emit_html(by_id[cid], 1)

html_out.append("</pre>")

# ─────────────────────────── write files ────────────────────────────────
pathlib.Path("room_log.txt").write_text("\n".join(txt_out) + "\n",  encoding="utf-8")
pathlib.Path("index.html" ).write_text("\n".join(html_out) + "\n", encoding="utf-8")

logging.info("archive written  ➜  index.html  &  room_log.txt  ✓")

