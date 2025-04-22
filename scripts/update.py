#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, subprocess, datetime, hashlib, html, colorsys
import collections, pathlib, logging
from datetime import datetime, timezone

# ─── logging ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s: %(message)s",
                    stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"                # mute nio spam

# ─── env (keep token secret) ────────────────────────────────────────────
hs   = os.environ["MATRIX_HS"]
uid  = os.environ["MATRIX_USER"]
rid  = os.environ["MATRIX_ROOM"]
tok  = os.environ["MATRIX_TOKEN"]
logging.debug(f"homeserver={hs} user={uid} room={rid}")

# ─── credentials ────────────────────────────────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)

if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver":   hs,
        "user_id":      uid,
        "access_token": tok,
        "device_id":    "GH",
        "room_id":      rid,
        "default_room": rid
    }))

cred_opts = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ─── helpers ────────────────────────────────────────────────────────────
def _stream(proc):
    """yield each line from proc, teeing to DEBUG log"""
    for line in proc:
        logging.debug(line.rstrip("\n"))
        yield line

def run_json(*args):
    """
    Run matrix‑commander:
      * everything written to **stderr** is streamed to DEBUG (human text);
      * **stdout** is returned, *but* we also log it line‑by‑line.
    """
    logging.debug("⟹ " + " ".join(args))
    proc = subprocess.Popen(args, text=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)

    # pump stderr in the background so it doesn’t dead‑lock
    for l in _stream(proc.stderr):
        pass

    stdout = []
    for l in _stream(proc.stdout):
        stdout.append(l)
    ret = proc.wait()
    if ret:
        raise subprocess.CalledProcessError(ret, args)
    return "".join(stdout)

def json_lines(raw: str):
    """return list of json objects; silently skip non‑json lines"""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or (line[0] not in "{["):
            continue            # human rubbish – ignore
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logging.debug(f"skipped non‑json line: {line[:60]}")
    return out

pastel = lambda u: "#{:02x}{:02x}{:02x}".format(
        *[int(c*255) for c in colorsys.hls_to_rgb(
            int(hashlib.sha1(u.encode()).hexdigest()[:8],16)/0xffffffff,
            0.70, 0.45)])
when   = lambda ev: datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)

# ─── make sure we are in the room ───────────────────────────────────────
try:
    run_json("matrix-commander", *cred_opts, "--room-join", rid)
except subprocess.CalledProcessError:
    pass  # already joined / not needed

# ─── room meta (best effort) ────────────────────────────────────────────
pretty = rid
try:
    meta_j = json_lines(run_json("matrix-commander", *cred_opts,
                                 "--room", rid,
                                 "--get-room-info", "--output", "json"))
    if meta_j:
        meta   = meta_j[0]
        pretty = next((meta.get(k) for k in (
            "room_display_name","room_name","room_canonical_alias","room_alias")
            if meta.get(k)), rid)
except Exception as e:
    logging.warning(f"meta fetch failed: {e}")

# ─── grab messages ──────────────────────────────────────────────────────
TAIL_N = os.getenv("TAIL_N", "20000")
raw = run_json("matrix-commander", *cred_opts,
               "--room", rid, "--listen", "all",
               "--listen-self", "--output", "json")

events = [e for e in json_lines(raw) if e.get("type") == "m.room.message"]
logging.info(f"parsed {len(events)} m.room.message events")

if not events:
    logging.error("zero events – nothing to archive?")
    sys.exit(1)

# ─── thread bookkeeping ────────────────────────────────────────────────
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
    "<style>body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#eee;padding:1em}"
    "time{color:#888;margin-right:.5em}.u{font-weight:600}.reply{margin-left:2ch}"
    "a{color:#9cf;text-decoration:none}</style>",
    f"<h1>{html.escape(pretty)}</h1>",
    "<p><a href='room_log.txt'>⇩ download plaintext</a></p>",
    "<hr><pre>"
]

def emit(ev, indent=""):
    t,u,b = when(ev).isoformat()+"Z", ev["sender"], ev["content"].get("body","")
    txt  .append(f"{indent}{t} {u}: {b}")
    html.append(
        f"<div class='{('reply' if indent else '')}'>"
        f"<time>{t}</time>"
        f"<span class='u' style='color:{pastel(u)}'>{html.escape(u)}</span>: "
        f"{html.escape(b)}</div>")

for root in roots:
    emit(root)
    for cid in sorted(threads[root["event_id"]], key=lambda x: when(by_id[x])):
        emit(by_id[cid], indent="  ")

html.append("</pre>")

pathlib.Path("room_log.txt").write_text("\n".join(txt)+"\n", encoding="utf8")
pathlib.Path("index.html" ).write_text("\n".join(html),        encoding="utf8")

logging.info("archive written to index.html & room_log.txt ✓")

