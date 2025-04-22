#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, subprocess, datetime, hashlib, html, colorsys, collections, sys

# ---- env ----
hs, uid, rid, tok = (os.environ[k] for k in
    ("MATRIX_HS", "MATRIX_USER", "MATRIX_ROOM", "MATRIX_TOKEN"))

def sh(*args):
    return subprocess.check_output(args, text=True)

# ---- pull room meta (name / alias) ----
meta = {}
try:
    meta_json = sh(
        "matrix-commander",
        "--access-token", tok,
        "--homeserver",   hs,
        "--login",   uid,
        "--room",         rid,
        "--get-room-info",
        "--output",       "json",
    )
    meta = json.loads(meta_json)[0]  # commander spits list
except Exception as e:
    print("meta fetch failed:", e, file=sys.stderr)

title = meta.get("room_display_name") or meta.get("room_name")
alias = meta.get("room_canonical_alias") or meta.get("room_alias")
pretty = title or alias or rid      # fallbacks chained

# ---- yank history ----
events = json.loads(sh(
    "matrix-commander",
    "--access-token", tok,
    "--homeserver",   hs,
    "--user-login",   uid,
    "--room",         rid,
    "--listen",       "all",
    "--output",       "json"
))

# ---- thread index ----
by_id, threads = {}, collections.defaultdict(list)
for ev in events:
    if ev.get("type") != "m.room.message":
        continue
    eid = ev["event_id"]
    by_id[eid] = ev
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        threads[rel["event_id"]].append(eid)

def ts(ev):
    return datetime.datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)

def pastel(user):
    h = int(hashlib.sha1(user.encode()).hexdigest()[:8],16)/0xffffffff
    r,g,b = colorsys.hls_to_rgb(h, 0.7, 0.45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

roots = [e for e in by_id.values() if e["event_id"]
         not in {c for kids in threads.values() for c in kids}]
roots.sort(key=ts)

# ---- emit txt + html ----
stamp = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

txt_out  = [
    f"# room: {pretty}",
    f"# exported: {stamp}",
]
html_out = [
    "<!doctype html><meta charset='utf-8'>",
    f"<title>{html.escape(pretty)} archive</title>",
    "<style>",
    "body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#e5e5e5;padding:1em}",
    "h1{margin-top:0;font:20px/1.2 ui-monospace,monospace}",
    "time{color:#888;margin-right:.5em}",
    ".u{font-weight:600}",
    ".reply{margin-left:2ch}",
    "a{color:#9cf;text-decoration:none}",
    "</style>",
    f"<h1>{html.escape(pretty)}</h1>",
    f"<p><a href='room_log.txt'>⇩ download plaintext</a></p>",
    "<hr><pre>"
]

def emit(ev, indent=""):
    t = ts(ev).isoformat() + "Z"
    u = ev["sender"]
    b = ev["content"].get("body", "")
    txt_out.append(f"{indent}{t} {u}: {b}")
    html_out.append(
        f"<div class='{ 'reply' if indent else '' }'>"
        f"<time>{t}</time>"
        f"<span class='u' style='color:{pastel(u)}'>{html.escape(u)}</span>: "
        f"{html.escape(b)}</div>"
    )

for root in roots:
    emit(root)
    for cid in sorted(threads[root["event_id"]], key=lambda i: ts(by_id[i])):
        emit(by_id[cid], indent="  ")

html_out.append("</pre>")

# ---- write files ----
with open("room_log.txt","w",encoding="utf8") as f:
    f.write("\n".join(txt_out) + "\n")

with open("index.html","w",encoding="utf8") as f:
    f.write("\n".join(html_out))

