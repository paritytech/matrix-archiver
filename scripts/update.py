#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, subprocess, datetime, hashlib, html, colorsys, collections, pathlib, sys, logging
from datetime import datetime, timezone

# hush nio “one_time_key_counts” noise
os.environ["NIO_LOG_LEVEL"] = "error"
# we don't need encryption errors
logging.getLogger("nio").setLevel(logging.ERROR)

# ---------- env ----------
hs   = os.environ["MATRIX_HS"]
uid  = os.environ["MATRIX_USER"]
rid  = os.environ["MATRIX_ROOM"]          # '!roomid:homeserver'
tok  = os.environ["MATRIX_TOKEN"]

# ---------- creds ----------
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store")
store_dir.mkdir(exist_ok=True)

if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver":   hs,
        "user_id":      uid,
        "access_token": tok,
        "device_id":    "GH",
        "room_id":      rid,
        "default_room": rid
    }))

cred = ["--credentials", str(cred_file), "--store", str(store_dir)]

def run(*args):
    """invoke matrix‑commander, return stdout"""
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE)

def parse_lines(raw: str):
    return [json.loads(l) for l in raw.splitlines() if l.strip()]

def pastel(u):
    h = int(hashlib.sha1(u.encode()).hexdigest()[:8],16)/0xffffffff
    r,g,b = colorsys.hls_to_rgb(h, 0.70, 0.45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

def when(ev): return datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)

# ---------- join room (idempotent) ----------
try:
    run("matrix-commander", *cred, "--room-join", rid)
except subprocess.CalledProcessError:
    # already joined or join not allowed → ignore
    pass

# ---------- room meta ----------
pretty = rid
try:
    meta_raw = run("matrix-commander", *cred,
                   "--room", rid, "--get-room-info", "--output", "json")
    meta = parse_lines(meta_raw)[0]
    pretty = (meta.get("room_display_name") or meta.get("room_name") or
              meta.get("room_canonical_alias") or meta.get("room_alias") or rid)
except Exception as e:
    print("warn: meta fetch failed:", e, file=sys.stderr)

# ---------- fetch events ----------
tail_n = "10000"  # grab up to 10k msgs every run

raw = run("matrix-commander", *cred,
          "--room", rid, "--listen", "tail",
          "--tail", tail_n, "--listen-self",
          "--output", "json")

events = [e for e in parse_lines(raw) if e.get("type") == "m.room.message"]

# ---------- threads ----------
by_id, threads = {}, collections.defaultdict(list)
for ev in events:
    by_id[ev["event_id"]] = ev
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        threads[rel["event_id"]].append(ev["event_id"])

roots = sorted(
    [e for e in events if e["event_id"] not in
     {c for kids in threads.values() for c in kids}],
    key=when
)

# ---------- emit ----------
stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
txt  = [f"# room: {pretty}", f"# exported: {stamp}"]
html = [
    "<!doctype html><meta charset='utf-8'>",
    f"<title>{html.escape(pretty)} archive</title>",
    "<style>",
    "body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#e5e5e5;padding:1em}",
    "h1{margin:0 0 .5em;font:20px/1.3 ui-monospace}",
    "time{color:#888;margin-right:.5em}",
    ".u{font-weight:600}",
    ".reply{margin-left:2ch}",
    "a{color:#9cf;text-decoration:none}",
    "</style>",
    f"<h1>{html.escape(pretty)}</h1>",
    "<p><a href='room_log.txt'>⇩ download plaintext</a></p>",
    "<hr><pre>"
]

def emit(ev, indent=""):
    t = when(ev).isoformat()+"Z"
    u = ev["sender"]
    b = ev["content"].get("body","")
    txt.append(f"{indent}{t} {u}: {b}")
    html.append(
        f"<div class='{'reply' if indent else ''}'>"
        f"<time>{t}</time>"
        f"<span class='u' style='color:{pastel(u)}'>{html.escape(u)}</span>: "
        f"{html.escape(b)}</div>"
    )

for root in roots:
    emit(root)
    for cid in sorted(threads[root["event_id"]], key=lambda x: when(by_id[x])):
        emit(by_id[cid], indent="  ")

html.append("</pre>")

pathlib.Path("room_log.txt").write_text("\n".join(txt)+"\n", encoding="utf8")
pathlib.Path("index.html").write_text("\n".join(html),           encoding="utf8")

