#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, subprocess, datetime, hashlib, html, colorsys, collections, pathlib, sys

# ---------- env ----------
hs   = os.environ["MATRIX_HS"]
uid  = os.environ["MATRIX_USER"]
rid  = os.environ["MATRIX_ROOM"]          # MUST be '!roomid:hs'
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
        "default_room": rid        # makes --listen all happy
    }))

cred = ["--credentials", str(cred_file), "--store", str(store_dir)]

def run(*args):
    """wrapper: run commander, return stdout ('' possible)"""
    out = subprocess.check_output(args, text=True, stderr=subprocess.PIPE)
    return out

def safe_json(raw):
    return [] if not raw.strip() else json.loads(raw)

# ---------- room meta ----------
meta = {}
try:
    meta = safe_json(run("matrix-commander", *cred,
                         "--room", rid, "--get-room-info", "--output", "json"))[0]
except Exception as e:
    print("warn: couldn't fetch room meta:", e, file=sys.stderr)

pretty = (meta.get("room_display_name") or meta.get("room_name") or
          meta.get("room_canonical_alias") or meta.get("room_alias") or rid)

# ---------- fetch events ----------
events = safe_json(run("matrix-commander", *cred,
                       "--room", rid, "--listen", "all", "--output", "json"))

# ---------- index threads ----------
by_id, threads = {}, collections.defaultdict(list)
for ev in events:
    if ev.get("type") != "m.room.message":
        continue
    eid = ev["event_id"]
    by_id[eid] = ev
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        threads[rel["event_id"]].append(eid)

def when(ev): return datetime.datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)
def pastel(u):
    h = int(hashlib.sha1(u.encode()).hexdigest()[:8],16)/0xffffffff
    r,g,b = colorsys.hls_to_rgb(h, 0.7, 0.45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

roots = [e for e in by_id.values()
         if e["event_id"] not in {c for kids in threads.values() for c in kids}]
roots.sort(key=when)

# ---------- build outputs ----------
stamp = datetime.datetime.utcnow().isoformat(timespec="seconds")+"Z"
txt = [f"# room: {pretty}", f"# exported: {stamp}"]
html = [
    "<!doctype html><meta charset='utf-8'>",
    f"<title>{html.escape(pretty)} archive</title>",
    "<style>body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#e5e5e5;padding:1em}"
    "h1{margin:0 0 .5em 0;font:20px/1.3 ui-monospace,monospace}"
    "time{color:#888;margin-right:.5em}.u{font-weight:600}.reply{margin-left:2ch}"
    "a{color:#9cf;text-decoration:none}</style>",
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
        f"{html.escape(b)}</div>"
    )

for root in roots:
    emit(root)
    for cid in sorted(threads[root["event_id"]], key=lambda x: when(by_id[x])):
        emit(by_id[cid], indent="  ")

html.append("</pre>")

# ---------- write ----------
pathlib.Path("room_log.txt").write_text("\n".join(txt)+"\n", encoding="utf8")
pathlib.Path("index.html").write_text("\n".join(html),           encoding="utf8")

