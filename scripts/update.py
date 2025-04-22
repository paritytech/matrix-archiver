#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dump a (public, un‑encrypted) Matrix room to
    •  room_log.txt   (plain‑text)
    •  index.html     (colourised, links to txt)

Designed for CI / GitHub Actions: prints every byte the CLI writes,
but never leaks the access‑token.
"""

import os, sys, json, subprocess, datetime, hashlib, html, colorsys
import collections, pathlib, logging, shlex, signal
from datetime import datetime, timezone

# ─── configuration via env ──────────────────────────────────────────────
HS   = os.environ["MATRIX_HS"]
UID  = os.environ["MATRIX_USER"]
RID  = os.environ["MATRIX_ROOM"]
TOK  = os.environ["MATRIX_TOKEN"]

LISTEN_MODE = os.getenv("LISTEN_MODE", "all")   # tail | once | all
TAIL_N      = os.getenv("TAIL_N",      "20000")  # only used for tail
TIMEOUT_S   = int(os.getenv("LISTEN_TIMEOUT", "30"))   # safety for “all”

# ─── logging ────────────────────────────────────────────────────────────
logging.basicConfig(stream=sys.stderr,
                    format="%(levelname)s: %(message)s",
                    level=logging.DEBUG)
os.environ["NIO_LOG_LEVEL"] = "error"            # hush nio validator spam

logging.debug(f"homeserver={HS} user={UID} room={RID} mode={LISTEN_MODE}")

# ─── credentials file / store dir ───────────────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)

if not cred_file.exists():     # first run on fresh runner
    cred_file.write_text(json.dumps({
        "homeserver":   HS,
        "user_id":      UID,
        "access_token": TOK,
        "device_id":    "GH",
        "room_id":      RID,
        "default_room": RID
    }))

CRED = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ─── helpers ────────────────────────────────────────────────────────────
def _tee(stream):                      # generator: log + yield
    for line in stream:
        logging.debug(line.rstrip("\n"))
        yield line

def run_json(cmd, timeout=None):
    """
    Run *cmd* (iterable of args).  Every byte of stdout/stderr is streamed to
    DEBUG.  Stdout is also returned (string) for later JSON parsing.
    """
    logging.debug("⟹ " + " ".join(map(shlex.quote, cmd)))
    proc = subprocess.Popen(cmd, text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            bufsize=1)

    # pump stderr in background so it cannot fill the pipe and dead‑lock
    for _ in _tee(proc.stderr):
        pass

    try:
        stdout = "".join(_tee(proc.stdout))
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        logging.warning(f"matrix‑commander timed‑out after {timeout}s – killed")
    if proc.returncode not in (0, None):
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return stdout

def json_lines(blob: str):
    """Parse every *valid* JSON line; quietly ignore human chatter."""
    out = []
    for line in blob.splitlines():
        line = line.strip()
        if line and line[0] in "{[":
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logging.debug(f"skip non‑json line: {line[:40]}")
    return out

pastel = lambda u: "#{:02x}{:02x}{:02x}".format(*(
        int(c*255) for c in colorsys.hls_to_rgb(
            int(hashlib.sha1(u.encode()).hexdigest()[:8],16)/0xffffffff,
            0.70, 0.45)))
when = lambda ev: datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)

# ─── make sure we’re joined ─────────────────────────────────────────────
try:
    run_json(["matrix-commander", *CRED, "--room-join", RID])
except subprocess.CalledProcessError:
    pass      # already joined / join forbidden – fine for public room

# ─── pretty room name (best effort) ─────────────────────────────────────
pretty = RID
try:
    meta = json_lines(run_json(
        ["matrix-commander", *CRED,
         "--room", RID, "--get-room-info", "--output", "json"]))[0]
    for k in ("room_display_name","room_name",
              "room_canonical_alias","room_alias"):
        if meta.get(k):
            pretty = meta[k]; break
except Exception as e:
    logging.warning(f"room‑info failed: {e}")

# ─── harvest events ─────────────────────────────────────────────────────
listen_args = ["--room", RID, "--listen", LISTEN_MODE, "--listen-self",
               "--output", "json"]
if LISTEN_MODE == "tail":
    listen_args.extend(["--tail", TAIL_N])

raw = run_json(["matrix-commander", *CRED, *listen_args],
               timeout = (TIMEOUT_S if LISTEN_MODE == "all" else None))

events = [e for e in json_lines(raw) if e.get("type") == "m.room.message"]
logging.info(f"got {len(events)} m.room.message events")

if not events:
    logging.error("no events found – aborting archive")
    sys.exit(1)

# ─── thread linkage ─────────────────────────────────────────────────────
by_id, threads = {}, collections.defaultdict(list)
for ev in events:
    by_id[ev["event_id"]] = ev
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        threads[rel["event_id"]].append(ev["event_id"])

roots = sorted(
    [e for e in events
     if e["event_id"] not in {c for kids in threads.values() for c in kids}],
    key=when)

# ─── build outputs ──────────────────────────────────────────────────────
stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
txt  = [f"# room: {pretty}", f"# exported: {stamp}"]
html = [
    "<!doctype html><meta charset=utf-8>",
    f"<title>{html.escape(pretty)}</title>",
    "<style>body{{font:14px/1.45 ui-monospace,monospace;background:#111;color:#eee;padding:1em}}"
    "time{{color:#888;margin-right:.5em}}.u{{font-weight:600}}.reply{{margin-left:2ch}}"
    "a{{color:#9cf;text-decoration:none}}</style>".format(),
    f"<h1>{html.escape(pretty)}</h1>",
    "<p><a href='room_log.txt'>⇩ download plaintext</a></p>",
    "<hr><pre>"
]

def emit(ev, indent=""):
    t,u,b = when(ev).isoformat()+"Z", ev["sender"], ev["content"].get("body","")
    txt.append(f"{indent}{t} {u}: {b}")
    html.append(
        f"<div class='{'reply' if indent else ''}'>"
        f"<time>{t}</time>"
        f"<span class='u' style='color:{pastel(u)}'>{html.escape(u)}</span>: "
        f"{html.escape(b)}</div>")

for root in roots:
    emit(root)
    for cid in sorted(threads[root["event_id"]],
                      key=lambda x: when(by_id[x])):
        emit(by_id[cid], indent="  ")

html.append("</pre>")

pathlib.Path("room_log.txt").write_text("\n".join(txt)+"\n", encoding="utf8")
pathlib.Path("index.html" ).write_text("\n".join(html),        encoding="utf8")
logging.info("archive written  ✓  (index.html  +  room_log.txt)")

