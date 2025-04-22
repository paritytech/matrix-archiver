#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive one or *several* public, un‑encrypted Matrix rooms.

For every room we create:

    <slug>/index.html   — nice threaded HTML
    <slug>/room_log.txt — plaintext, optimized for LLMs

and a top‑level  index.html  that lists all archived rooms.
"""

import os, sys, re, json, subprocess, shlex, hashlib, colorsys, html, logging
import collections, pathlib
from datetime import datetime, timezone

# ────── configuration via env ───────────────────────────────────────────
HS       = os.environ["MATRIX_HS"]
USER_ID  = os.environ["MATRIX_USER"]
TOKEN    = os.environ["MATRIX_TOKEN"]

# rooms: MATRIX_ROOMS takes priority, else MATRIX_ROOM
rooms_raw = os.getenv("MATRIX_ROOMS") or os.getenv("MATRIX_ROOM") or ""
ROOMS = [r for r in re.split(r"[,\s]+", rooms_raw) if r]
if not ROOMS:
    sys.exit("❌  No room(s) specified (MATRIX_ROOM / MATRIX_ROOMS)")

LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()        # all|tail|once
TAIL_N      = os.getenv("TAIL_N", "20000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", 20))

# ────── logging (INFO by default) ───────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s: %(message)s", stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"       # silence nio crypto noise

# ────── credentials (shared for all rooms) ──────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)

if not cred_file.exists():                  # first run – create cred skeleton
    cred_file.write_text(json.dumps({
        "homeserver": HS, "user_id": USER_ID, "access_token": TOKEN,
        "device_id": "GH", "room_id": ROOMS[0], "default_room": ROOMS[0],
    }))
CRED = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ────── helper utilities ────────────────────────────────────────────────
def pastel(uid: str) -> str:
    h = int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(h, 0.70, 0.45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev): return datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)
nice_user = lambda u: u.lstrip("@").split(":", 1)[0]

def json_lines(raw: str):
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln and ln[0] in "{[":
            try:
                yield json.loads(ln)
            except json.JSONDecodeError:
                pass

def run(cmd, timeout=None, quiet=False):
    if not quiet:
        logging.debug("⟹ %s", " ".join(map(shlex.quote, cmd)))
    res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if res.stderr and not quiet:
        for l in res.stderr.splitlines():
            logging.debug(l)
    if res.returncode not in (0, None):
        raise subprocess.CalledProcessError(res.returncode, cmd)
    return res.stdout

def slugify(s: str) -> str:
    """turn '#public:example.org' or '!abc:ex.org' → 'public' / 'abc'"""
    s = s.lstrip("#!").split(":", 1)[0]
    return re.sub(r"[^0-9A-Za-z._-]+", "_", s) or "room"

# ────── template bits (shared) ──────────────────────────────────────────
CSS = (
    "body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#eee;"
    "padding:1em}"
    ".msg{white-space:pre-wrap}"
    "time{color:#888;margin-right:.5em}"
    ".u{font-weight:600}"
    ".reply{margin-left:2ch}"
    "a{color:#9cf;text-decoration:none}"
)

listen_args = {
    "all":  ["--listen", "all",  "--listen-self"],
    "tail": ["--listen", "tail", "--tail", TAIL_N, "--listen-self"],
    "once": ["--listen", "once", "--listen-self"],
}[LISTEN_MODE]

# ────── archive one room ────────────────────────────────────────────────
def archive_room(room_id: str):
    logging.info("↻  %s", room_id)

    # 1) ensure we are a member (idempotent)
    try: run(["matrix-commander", *CRED, "--room-join", room_id], quiet=True)
    except subprocess.CalledProcessError:
        pass

    # 2) fetch human‑friendly title
    title = room_id
    try:
        meta = next(json_lines(run(
            ["matrix-commander", *CRED,
             "--room", room_id, "--get-room-info", "--output", "json"],
            quiet=True)), {})
        for k in ("display_name","name","room_display_name","room_name",
                  "room_canonical_alias","room_alias"):
            if meta.get(k):
                title = meta[k]; break
    except Exception as e:
        logging.warning("meta fetch failed for %s: %s", room_id, e)

    # 3) fetch events
    raw = run(
        ["matrix-commander", *CRED, "--room", room_id, *listen_args,
         "--output", "json"],
        timeout=TIMEOUT_S if LISTEN_MODE == "all" else None,
        quiet=True
    )

    events = []
    for j in json_lines(raw):
        ev = j.get("source", j)
        if ev.get("type") == "m.room.message":
            events.append(ev)

    logging.info("   %d messages", len(events))
    if not events:
        logging.warning("   (skipped – no messages)")
        return None           # nothing to write

    # 4) build thread maps
    by_id = {e["event_id"]: e for e in events}
    threads = collections.defaultdict(list)
    for e in events:
        rel = e["content"].get("m.relates_to", {})
        if rel.get("rel_type") == "m.thread":
            threads[rel["event_id"]].append(e["event_id"])

    roots = sorted(
        [e for e in events if e["event_id"] not in
         {c for kids in threads.values() for c in kids}],
        key=when
    )

    # 5) output paths
    room_slug = slugify(title)
    out_dir   = pathlib.Path(room_slug)
    out_dir.mkdir(exist_ok=True)
    txt_file  = out_dir / "room_log.txt"
    html

