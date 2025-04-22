#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, subprocess, datetime, hashlib, html, colorsys, collections, pathlib, sys, tempfile

# ---------------- env ----------------
try:
    hs   = os.environ["MATRIX_HS"]
    uid  = os.environ["MATRIX_USER"]
    rid  = os.environ["MATRIX_ROOM"]
    tok  = os.environ["MATRIX_TOKEN"]
except KeyError as k:
    sys.exit(f"missing env: {k}")

# ---------------- minimal creds ----------------
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store")
store_dir.mkdir(exist_ok=True)

if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver":    hs,
        "user_id":       uid,
        "access_token":  tok,
        "device_id":     "GH",
        "default_room":  rid
    }))

cred_opts = ["--credentials", str(cred_file), "--store", str(store_dir)]

def run_cmd(args, **kw):
    return subprocess.check_output(args, text=True, **kw)

# ---------------- room meta ----------------
meta = {}
try:
    meta_json = run_cmd([
        "matrix-commander", *cred_opts,
        "--room", rid, "--get-room-info", "--output", "json"
    ])
    meta = json.loads(meta_json)[0]
except Exception as e:
    print("warn: room meta fetch failed:", e, file=sys.stderr)

title  = meta.get("room_display_name") or meta.get("room_name")
alias  = meta.get("room_canonical_alias") or meta.get("room_alias")
pretty = title or alias or rid

# ---------------- fetch events ----------------
events = json.loads(run_cmd([
    "matrix-commander", *cred_opts,
    "--room", rid, "--listen", "all", "--output", "json"
]))

# ---------------- index threads ----------------
by_id, threads = {}, collections.defaultdict(list)
for ev in events:
    if ev.get("type") != "m.room.message":
        continue
    eid = ev["event_id"]
    by_id[eid] = ev
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        threads[rel["event_id"]].append(eid)

def when(ev):
    return datetime.datetime.utcfromtimestamp(ev["origin_server_ts"] / 1000)

def pastel(user):
    h = int(hashlib.sha1(user.encode()).hexdigest()[:8], 16) / 0xffffffff
    r, g, b = colorsys.hls_to_rgb(h, 0.70, 0.45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

roots = [e for e in by_id.values()
         if e["event_id"] not in {c for kids in threads.values() for c in kids}]
roots.sort(key=when)

# ---------------- build outputs ----------------
stamp = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

txt_lines = [
    f"# room: {pretty}",
    f"# exported: {stamp}",
]

html_lines = [
    "<!doctype html><meta charset='utf-8'>",
    f"<title>{html.escape(pretty)} archive</title>",
    "<style>",
    "body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#e5e5e5;padding:1em}",
    "h1{margin:0 0 .5em 0;font:20px/1.3 ui-monospace,monospace}",
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
    t = when(ev).isoformat() + "Z"
    u = ev["sender"]
    body = ev["content"].get("body", "")
    txt_lines.append(f"{indent}{t} {u}: {body}")
    html_lines.append(
        f"<div class='{'reply' if indent else ''}'>"
        f"<time>{t}</time>"
        f"<span class='u' style='color:{pastel(u)}'>{html.escape(u)}</span>: "
        f"{html.escape(body)}</div>"
    )

for root in roots:
    emit(root)
    for cid in sorted(threads[root["event_id"]], key=lambda i: when(by_id[i])):
        emit(by_id[cid], indent="  ")

html_lines.append("</pre>")

# ---------------- write files ----------------
pathlib.Path("room_log.txt").write_text("\n".join(txt_lines) + "\n", encoding="utf8")
pathlib.Path("index.html").write_text("\n".join(html_lines), encoding="utf8")

