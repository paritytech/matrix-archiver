#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive one‑or‑many public Matrix rooms.
Creates archive/<slug>/index.html + room_log.txt and a root directory page.
"""

import os, sys, json, subprocess, shlex, hashlib, colorsys, html, logging, re
import collections, pathlib, urllib.parse
from datetime import datetime, timezone

# ────────── basic settings ────────────────────────────────────────────
HS        = os.environ["MATRIX_HS"]
USER_ID   = os.environ["MATRIX_USER"]
TOKEN     = os.environ["MATRIX_TOKEN"]
ROOMS_RAW = os.getenv("MATRIX_ROOMS") or os.getenv("MATRIX_ROOM", "")
ROOMS     = [r.strip() for r in re.split(r"[,\s]+", ROOMS_RAW) if r.strip()]
if not ROOMS:
    sys.exit("‼  No MATRIX_ROOMS specified")

LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()      # all|tail|once
TAIL_N      = os.getenv("TAIL_N", "10000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", 20))

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s: %(message)s", stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"

# ────────── credentials (shared) ───────────────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)
if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver":   HS,
        "user_id":      USER_ID,
        "access_token": TOKEN,
        "device_id":    "GH",
        "default_room": ROOMS[0],
    }))
CRED = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ────────── helpers ────────────────────────────────────────────────────
def run(cmd, timeout=None):
    logging.debug("⟹ %s", " ".join(map(shlex.quote, cmd)))
    res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    for l in res.stderr.splitlines(): logging.debug(l)
    if res.returncode:
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res.stdout

def json_lines(blob:str):
    for line in blob.splitlines():
        line=line.strip()
        if line and line[0] in "{[":
            try: yield json.loads(line)
            except json.JSONDecodeError: pass

when      = lambda e: datetime.utcfromtimestamp(e["origin_server_ts"]/1000)
nice_user = lambda u: u.lstrip("@").split(":",1)[0]
def pastel(u):
    h=int(hashlib.sha1(u.encode()).hexdigest()[:8],16)/0xffffffff
    r,g,b=colorsys.hls_to_rgb(h,.70,.45); return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
slug = lambda s: urllib.parse.quote(s, safe="").replace("%","_")

# ────────── archive one room ───────────────────────────────────────────
def archive_room(room:str):
    logging.info("room: %s", room)

    # ── put this room into the credentials file so matrix‑commander is happy
    cred = json.loads(cred_file.read_text())
    cred["room_id"]      = room
    cred["default_room"] = room
    cred_file.write_text(json.dumps(cred))

    room_dir = pathlib.Path("archive")/slug(room)
    room_dir.mkdir(parents=True, exist_ok=True)

    try: run(["matrix-commander", *CRED, "--room-join", room])
    except subprocess.CalledProcessError: pass

    # mini‑sync
    try: run(["matrix-commander", *CRED, "--room", room, "--listen", "once"])
    except subprocess.CalledProcessError: pass

    # title
    title = room
    try:
        info = next(json_lines(run(["matrix-commander", *CRED,
                                    "--room", room, "--get-room-info",
                                    "--output", "json"])), {})
        for k in ("room_display_name","room_name","canonical_alias","room_alias"):
            if info.get(k): title = info[k]; break
    except Exception as e:
        logging.warning("  get‑room‑info failed – %s", e)

    # messages
    listen = {"all":["--listen","all","--listen-self"],
              "tail":["--listen","tail","--tail",TAIL_N,"--listen-self"],
              "once":["--listen","once","--listen-self"]}[LISTEN_MODE]
    raw = run(["matrix-commander", *CRED, "--room", room, *listen, "--output","json"],
              timeout=TIMEOUT_S if LISTEN_MODE=="all" else None)

    events=[ev for j in json_lines(raw)
              for ev in [(j.get("source", j))] if ev.get("type")=="m.room.message"]
    logging.info("  messages: %d", len(events))
    if not events: return

    by_id={e["event_id"]:e for e in events}
    threads=collections.defaultdict(list)
    for e in events:
        rel=e["content"].get("m.relates_to",{})
        if rel.get("rel_type")=="m.thread":
            threads[rel["event_id"]].append(e["event_id"])
    roots=sorted([e for e in events if e["event_id"] not in
                  {c for kids in threads.values() for c in kids}], key=when)

    # plaintext
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
    txt=[f"# room: {title}",f"# exported: {stamp}"]
    def add_txt(ev,lvl):
        arrow="↳ " if lvl else ""
        txt.append(f"{'  '*lvl}{arrow}{when(ev).strftime('%Y-%m-%d %H:%M')} "
                   f"{nice_user(ev['sender'])}: {ev['content'].get('body','')}")
    for r in roots:
        add_txt(r,0)
        for cid in sorted(threads[r["event_id"]], key=lambda i: when(by_id[i])):
            add_txt(by_id[cid],1)

    # html
    html_lines=[
        "<!doctype html><meta charset=utf-8>",
        f"<title>{html.escape(title)} – archive</title>",
        "<style>body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#eee;padding:1em}"
        ".msg{white-space:pre-wrap}time{color:#888;margin-right:.5em}"
        ".u{font-weight:600}.reply{margin-left:2ch}a{color:#9cf;text-decoration:none}</style>",
        f"<h1>{html.escape(title)}</h1>",
        "<p><a href='room_log.txt'>⇩ plaintext</a>  ·  <a href='../index.html'>all rooms</a></p>",
        "<hr>",
    ]
    def add_html(ev,lvl):
        cls="msg reply" if lvl else "msg"
        html_lines.append(
            f"<div class='{cls}'>"
            f"<time>{when(ev).strftime('%Y‑%m‑%d %H:%M')}</time>&ensp;"
            f"<span class='u' style='color:{pastel(ev['sender'])}'>{nice_user(ev['sender'])}</span>: "
            f"{html.escape(ev['content'].get('body',''))}</div>")
    for r in roots:
        add_html(r,0)
        for cid in sorted(threads[r["event_id"]], key=lambda i: when(by_id[i])):
            add_html(by_id[cid],1)

    (room_dir/"room_log.txt").write_text("\n".join(txt)+"\n", encoding="utf-8")
    (room_dir/"index.html" ).write_text("\n".join(html_lines)+"\n", encoding="utf-8")
    logging.info("  written → %s", room_dir)

# ────────── main ───────────────────────────────────────────────────────
pathlib.Path("archive").mkdir(exist_ok=True)
for r in ROOMS:
    try: archive_room(r)
    except Exception as exc:
        logging.error("‼ failed for %s – %s", r, exc)

# directory page
rooms_li="\n".join(
    f"<li><a href='archive/{slug(r)}/index.html'>{html.escape(r)}</a></li>" for r in ROOMS)
pathlib.Path("index.html").write_text(
    "\n".join([
        "<!doctype html><meta charset=utf-8>",
        "<title>Matrix room archive</title>",
        "<style>body{font:15px ui-monospace,monospace;background:#111;color:#eee;padding:1em}"
        "a{color:#9cf;text-decoration:none}</style>",
        "<h1>Archived rooms</h1><ul>",rooms_li,"</ul>"
    ])+"\n", encoding="utf-8")
logging.info("root index.html regenerated ✓")

