#!/usr/bin/env python3
import json, os, subprocess, datetime, hashlib, html, colorsys, collections

hs, uid, rid, tok = (os.environ[k] for k in
    ("MATRIX_HS","MATRIX_USER","MATRIX_ROOM","MATRIX_TOKEN"))

cmd = [
    "matrix-commander",
    "--access-token", tok,
    "--homeserver",   hs,
    "--user-login",   uid,
    "--room",         rid,
    "--listen",       "all",
    "--output",       "json"
]
events = json.loads(subprocess.check_output(cmd))

# ---------- pass 0: stash everything ----------
by_id   = {}
threads = collections.defaultdict(list)   # root_id → [child_ids]

for ev in events:
    if ev.get("type") != "m.room.message":
        continue
    eid = ev["event_id"]
    by_id[eid] = ev

    # threading spec: m.relates_to.rel_type == "m.thread"
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        root = rel["event_id"]
        threads[root].append(eid)

# ---------- helpers ----------
def ts(ev):   # datetime obj
    return datetime.datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)

def color(user):
    h = int(hashlib.sha1(user.encode()).hexdigest()[:8],16)/0xffffffff
    r,g,b = colorsys.hls_to_rgb(h, 0.7, 0.45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

# ---------- pass 1: emit ----------
roots = [e for e in by_id.values() if e["event_id"] not in
         {cid for kids in threads.values() for cid in kids}]
roots.sort(key=ts)

txt_lines  = []
html_lines = [
    "<!doctype html><meta charset='utf-8'>",
    "<style>body{font:14px ui-monospace;background:#111;color:#e5e5e5;padding:1em}"
    "time{color:#888;margin-right:.5em}.u{font-weight:600}"
    ".reply{margin-left:2ch}</style><pre>"
]

def emit(ev, indent=""):
    t  = ts(ev).isoformat()+"Z"
    u  = ev["sender"]
    b  = ev["content"].get("body","")
    txt_lines.append(f"{indent}{t} {u}: {b}")
    html_lines.append(
        f"<div class='{ 'reply' if indent else '' }'>"
        f"<time>{t}</time>"
        f"<span class='u' style='color:{color(u)}'>{html.escape(u)}</span>: "
        f"{html.escape(b)}</div>")

for root in roots:
    emit(root)
    for cid in sorted(threads[root["event_id"]], key=lambda i: ts(by_id[i])):
        emit(by_id[cid], indent="  ")

html_lines.append("</pre>")

# ---------- write files ----------
open("room_log.txt","w",encoding="utf8").write("\n".join(txt_lines)+"\n")
open("index.html","w",encoding="utf8").write("\n".join(html_lines))

