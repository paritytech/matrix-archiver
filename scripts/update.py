#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive one Matrix room into   • index.html   • room_log.txt
Intended for non‑encrypted, world‑readable rooms, run inside CI.
"""

import os, sys, json, subprocess, shlex, hashlib, colorsys, html, logging
import collections, pathlib, textwrap
from datetime import datetime, timezone

# ────────── configuration (env‑vars) ────────────────────────────────────
HS   = os.environ["MATRIX_HS"]
USER = os.environ["MATRIX_USER"]
ROOM = os.environ["MATRIX_ROOM"]          # '!roomid:homeserver'
TOKEN= os.environ["MATRIX_TOKEN"]

LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()   # all | tail | once
TAIL_N      = os.getenv("TAIL_N", "20000")              # if LISTEN_MODE=tail
TIMEOUT_S   = int(os.getenv("TIMEOUT", 20))             # only for mode=all

# ────────── logging ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s: %(message)s",
                    stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"       # mute nio crypto warnings

logging.debug(f"homeserver={HS}  user={USER}  room={ROOM}")

# ────────── credentials file / store dir ───────────────────────────────
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

# ────────── helpers ─────────────────────────────────────────────────────
def pastel(uid: str) -> str:
    h = int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(h, .70, .45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev):                                   # --> datetime (UTC)
    return datetime.utcfromtimestamp(ev["origin_server_ts"] / 1000.0)

def json_lines(raw: str):
    """Return JSON objects; silently ignore non‑JSON lines."""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logging.debug(f"skipped non‑json line: {line[:80]}")
    return out

def _stream_lines(stream):
    for ln in stream:
        logging.debug(ln.rstrip())
        yield ln

def run_stream(cmd: list[str]) -> str:
    """Run *matrix‑commander*, stream both stdio to DEBUG, return stdout."""
    logging.debug("⟹ " + " ".join(map(shlex.quote, cmd)))
    proc = subprocess.Popen(cmd, text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    # pump stderr in background
    for _ in _stream_lines(proc.stderr):
        pass
    out_buf = []
    for ln in _stream_lines(proc.stdout):
        out_buf.append(ln)
    ret = proc.wait()
    if ret:
        raise subprocess.CalledProcessError(ret, cmd)
    return "".join(out_buf)

def run_with_timeout(cmd: list[str], timeout_s: int) -> str:
    """Run cmd, kill after timeout_s, return stdout (log everything)."""
    logging.debug("⟹ " + " ".join(map(shlex.quote, cmd)))
    try:
        res = subprocess.run(cmd, text=True, capture_output=True,
                             timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        # coreutils ‘timeout’ style exit‑code (124) for consistency
        logging.warning(f"command exceeded {timeout_s}s – killed")
        res = exc
    for ln in res.stderr.splitlines():
        logging.debug(ln)
    for ln in res.stdout.splitlines():
        logging.debug(ln)
    return res.stdout

# ────────── ensure we’re a member of the room ───────────────────────────
try:
    run_stream(["matrix-commander", *CRED, "--room-join", ROOM])
except subprocess.CalledProcessError:
    pass                             # already joined / not allowed – ignore

# ────────── room pretty‑name (best effort) ──────────────────────────────
pretty = ROOM
try:
    meta = json_lines(run_stream(
        ["matrix-commander", *CRED,
         "--room", ROOM, "--get-room-info", "--output", "json"]))
    if meta:
        meta = meta[0]
        for key in ("room_display_name", "room_name",
                    "room_canonical_alias", "room_alias"):
            if meta.get(key):
                pretty = meta[key]; break
except Exception as e:
    logging.warning(f"meta fetch failed: {e}")

# ────────── fetch events ────────────────────────────────────────────────
listen_args = {
    "all" : ["--listen", "all",  "--listen-self"],
    "tail": ["--listen", "tail", "--tail", TAIL_N, "--listen-self"],
    "once": ["--listen", "once", "--listen-self"],
}[LISTEN_MODE]

if LISTEN_MODE == "all":
    raw = run_with_timeout(
        ["matrix-commander", *CRED, "--room", ROOM,
         *listen_args, "--output", "json"],
        TIMEOUT_S)
else:
    raw = run_stream(
        ["matrix-commander", *CRED, "--room", ROOM,
         *listen_args, "--output", "json"])

events = [e for e in json_lines(raw) if e.get("type") == "m.room.message"]
logging.info(f"parsed {len(events)} m.room.message events")

if not events:
    logging.error("no events – nothing to archive")
    sys.exit(1)

# ────────── thread bookkeeping ──────────────────────────────────────────
by_id   : dict[str, dict]           = {}
threads : dict[str, list[str]]      = collections.defaultdict(list)

for ev in events:
    by_id[ev["event_id"]] = ev
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        threads[rel["event_id"]].append(ev["event_id"])

root_events = sorted(
    [e for e in events
     if e["event_id"] not in {c for kids in threads.values() for c in kids}],
    key=when)

# ────────── build plaintext & html outputs ──────────────────────────────
stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")\
         .replace("+00:00", "Z")

txt_lines  = [f"# room: {pretty}", f"# exported: {stamp}"]
html_lines = [
    "<!doctype html><meta charset=utf-8>",
    f"<title>{html.escape(pretty)}</title>",
    "<style>",
    "body{font:14px/1.45 ui-monospace,monospace;"
    "background:#111;color:#eee;padding:1em}",
    "time{color:#888;margin-right:.5em}",
    ".u{font-weight:600}",
    ".reply{margin-left:2ch}",
    "a{color:#9cf;text-decoration:none}",
    "</style>",
    f"<h1>{html.escape(pretty)}</h1>",
    "<p><a href='room_log.txt'>⇩ download plaintext</a></p>",
    "<hr><pre>",
]

def emit(ev, indent: str = "") -> None:
    ts  = when(ev).isoformat() + "Z"
    usr = ev["sender"]
    body= ev["content"].get("body", "")
    txt_lines.append(f"{indent}{ts} {usr}: {body}")
    html_lines.append(
        f"<div class='{('reply' if indent else '')}'>"
        f"<time>{ts}</time>"
        f"<span class='u' style='color:{pastel(usr)}'>{html.escape(usr)}</span>: "
        f"{html.escape(body)}</div>")

for root in root_events:
    emit(root)
    for cid in sorted(threads[root["event_id"]], key=lambda i: when(by_id[i])):
        emit(by_id[cid], indent="  ")

html_lines.append("</pre>")

# ────────── write files ─────────────────────────────────────────────────
pathlib.Path("room_log.txt").write_text("\n".join(txt_lines)  + "\n",
                                        encoding="utf-8")
pathlib.Path("index.html" ).write_text("\n".join(html_lines)+ "\n",
                                        encoding="utf-8")

logging.info("archive written: index.html  +  room_log.txt   ✓")

