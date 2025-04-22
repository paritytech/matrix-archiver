#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, subprocess, datetime, hashlib, html, colorsys, collections, pathlib, logging
from datetime import datetime, timezone

# ─── logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,      # always noisy
    format="%(levelname)s: %(message)s",
    stream=sys.stderr)

os.environ["NIO_LOG_LEVEL"] = "error"      # mute crypto key‑count spam

# ─── env (don’t print token) ────────────────────────────────────────────
hs, uid, rid, tok = (
    os.environ["MATRIX_HS"],
    os.environ["MATRIX_USER"],
    os.environ["MATRIX_ROOM"],
    os.environ["MATRIX_TOKEN"],
)
logging.debug(f"homeserver={hs} user={uid} room={rid}")

# ─── creds ──────────────────────────────────────────────────────────────
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

cred = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ─── commander helper: stream live + capture ────────────────────────────
def run_cmd(*args):
    logging.debug("⟹ " + " ".join(args))
    proc = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = []
    for line in proc.stdout:
        logging.debug(line.rstrip("\n"))
        out.append(line)
    ret = proc.wait()
    if ret:
        raise subprocess.CalledProcessError(ret, args)
    return "".join(out)

parse_lines = lambda raw: [json.loads(l) for l in raw.splitlines() if l.strip()]
pastel      = lambda u: "#{:02x}{:02x}{:02x}".format(
    *[int(c*255) for c in colorsys.hls_to_rgb(
        int(hashlib.sha1(u.encode()).hexdigest()[:8],16)/0xffffffff, 0.70, 0.45)])
when        = lambda ev: datetime.utcfromtimestamp(ev["origin_server_ts"]/1000)

# ─── ensure joined ──────────────────────────────────────────────────────
try:   run_cmd("matrix-commander", *cred, "--room-join", rid)
except subprocess.CalledProcessError:
    pass

# ─── room meta (best‑effort) ────────────────────────────────────────────
pretty = rid
try:
    meta = parse_lines(run_cmd("matrix-commander", *cred,
                               "--room", rid, "--get-room-info", "--output", "json"))[0]
    pretty = next((meta.get(k) for k in (
        "room_display_name","room_name","room_canonical_alias","room_alias") if meta.get(k)), rid)
except Exception as e:
    logging.warning(f"meta fetch failed: {e}")

# ─── fetch messages – stateless tail page ───────────────────────────────
TAIL_N = os.getenv("TAIL_N", "20000")      # bump as you like
raw = run_cmd("matrix-commander", *cred,
              "--room", rid, "--listen", "tail", "--tail", TAIL_N,
              "--listen-self", "--output", "json")

events = [e for e in parse_lines(raw) if e.get("type") == "m.room.message"]
logging.info(f"fetched {len(events)} events")

if not events:
    logging.error("zero events returned – room empty or hs visibility blocking us")
    sys.exit(1)

# ─── threading split ────────────────────────────────────────────────────
by_id, threads = {}, collections.defaultdict(list)
for ev in events:
    by_id[ev["event_id"]] = ev
    rel = ev["content"].get("m.relates_to", {})
    if rel.get("rel_type") == "m.thread":
        threads[rel["event_id"]].append(ev["event_id"])

roots = sorted(
    [e for e in events if e["event_id"] not in
     {c for kids in threads.values() for c in kids}],
    key=when)

# ─── generate outputs ───────────────────────────────────────────────────
stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
txt  = [f"# room: {pretty}", f"# exported: {stamp}"]
html_lines = [
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
    txt.append(f"{indent}{t} {u}: {b}")
    html_lines.append(
        f"<div class='{('reply' if indent else '')}'>"
        f"<time>{t}</time>"
        f"<span class='u' style='color:{pastel(u)}'>{html.escape(u)}</span>: "
        f"{html.escape(b)}</div>")

for root in roots:
    emit(root)
    for cid in sorted(threads[root["event_id"]], key=lambda x: when(by_id[x])):
        emit(by_id[cid], indent="  ")

html_lines.append("</pre>")

pathlib.Path("room_log.txt").write_text("\n".join(txt)+"\n", encoding="utf8")
pathlib.Path("index.html").write_text("\n".join(html_lines), encoding="utf8")

logging.info("archive written to index.html + room_log.txt ✓")

