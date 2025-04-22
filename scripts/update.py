#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mirror one or many **public, un‑encrypted** Matrix rooms into flat text and a
minimal HTML log suitable for GitHub‑Pages.

Output tree (created/updated on every run) ⤵
archive/
 ├─ index.html                   – room directory listing
 └─ <room‑slug>/
     ├─ index.html               – pretty log
     └─ room_log.txt             – plaintext for LLMs / grep
"""

import os, sys, json, subprocess, shlex, hashlib, colorsys, html, logging
import collections, pathlib, re, textwrap
from datetime import datetime, timezone

# ─── configuration from env ─────────────────────────────────────────────
HS        = os.environ["MATRIX_HS"]                 # https://…
USER_ID   = os.environ["MATRIX_USER"]               # @bot:homeserver
TOKEN     = os.environ["MATRIX_TOKEN"]              # *access token*
ROOMS_VAR = os.getenv("MATRIX_ROOMS") or os.getenv("MATRIX_ROOM", "")

if not ROOMS_VAR.strip():
    sys.exit("❌  No rooms specified via MATRIX_ROOMS")

# split on comma / whitespace
ROOMS = re.split(r"[,\s]+", ROOMS_VAR.strip())

# archiver behaviour
LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()   # all | tail | once
TAIL_N      = os.getenv("TAIL_N", "20000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", 30))

# output root
DST_DIR = pathlib.Path("archive")
DST_DIR.mkdir(exist_ok=True)

# ─── logging / noise control ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)
os.environ["NIO_LOG_LEVEL"] = "error"       # silence matrix‑nio crypto debug

# ─── shared credentials / store ─────────────────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)

if not cred_file.exists():          # first run → create creds file
    cred_file.write_text(json.dumps({
        "homeserver":   HS,
        "user_id":      USER_ID,
        "access_token": TOKEN,
        "device_id":    "GH",
        "room_id":      "",
        "default_room": "",
    }))

CRED = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ─── tiny util helpers ──────────────────────────────────────────────────
def run(cmd, timeout=None) -> str:
    """Run a command, return stdout, always log stderr."""
    logging.debug("⟹ %s", " ".join(map(shlex.quote, cmd)))
    res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    for ln in res.stderr.splitlines():
        logging.debug(ln)
    res.check_returncode()
    return res.stdout

def json_lines(raw: str):
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln and ln[0] in "{[":
            try:
                yield json.loads(ln)
            except json.JSONDecodeError:
                logging.debug("skip: %s…", ln[:80])

def pastel(uid: str) -> str:
    h = int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(h, .70, .45)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

nice_user = lambda u: u.lstrip("@").split(":", 1)[0]
when       = lambda ev: datetime.utcfromtimestamp(ev["origin_server_ts"] / 1000)

def slug(room_id: str, alias: str | None) -> str:
    if alias:
        return re.sub(r"[^\w\-]", "_", alias.lstrip("#"))
    return hashlib.sha1(room_id.encode()).hexdigest()[:12]

# ─── main per‑room routine ──────────────────────────────────────────────
def archive_room(room_id_or_alias: str):
    logging.info("room ▶ %s", room_id_or_alias)

    # 1. join (idempotent)
    try:
        run(["matrix-commander", *CRED, "--room-join", room_id_or_alias])
    except subprocess.CalledProcessError:
        pass                          # already joined or join not allowed

    # 2. get meta info
    meta_raw = run([
        "matrix-commander", *CRED,
        "--room", room_id_or_alias, "--get-room-info", "--output", "json"
    ])
    meta = next(json_lines(meta_raw), {})
    title = meta.get("room_display_name") or meta.get("name") \
        or meta.get("canonical_alias") or room_id_or_alias
    c_alias = meta.get("canonical_alias")

    out_dir = DST_DIR / slug(meta.get("room_id", room_id_or_alias), c_alias)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 3. fetch messages --------------------------------------------------
    listen = {
        "all":  ["--listen", "all",  "--listen-self"],
        "tail": ["--listen", "tail", "--tail", TAIL_N, "--listen-self"],
        "once": ["--listen", "once", "--listen-self"],
    }[LISTEN_MODE]

    raw = run(
        ["matrix-commander", *CRED,
         "--room", room_id_or_alias, *listen, "--output", "json"],
        timeout=TIMEOUT_S if LISTEN_MODE == "all" else None
    )

    events = []
    for j in json_lines(raw):
        ev = j.get("source", j)
        if ev.get("type") == "m.room.message":
            events.append(ev)

    logging.info("  ↳ %d message events", len(events))
    if not events:
        return                                      # nothing to write

    # 4. thread bookkeeping ---------------------------------------------
    by_id = {e["event_id"]: e for e in events}
    threads = collections.defaultdict(list)
    for e in events:
        rel = e["content"].get("m.relates_to", {})
        if rel.get("rel_type") == "m.thread":
            threads[rel["event_id"]].append(e["event_id"])

    roots = sorted(
        [e for e in events
         if e["event_id"] not in
            {cid for kids in threads.values() for cid in kids}],
        key=when
    )

    # 5. write plaintext -------------------------------------------------
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")[:-6] + "Z"
    plain_lines = [f"# room: {title}", f"# exported: {stamp}"]

    def add_plain(ev, lvl):
        ts = when(ev).strftime("%Y-%m-%d %H:%M")
        prefix = ("  " * lvl) + ("↳ " if lvl else "")
        body = ev["content"].get("body", "")
        plain_lines.append(f"{prefix}{ts} {nice_user(ev['sender'])}: {body}")

    for r in roots:
        add_plain(r, 0)
        for cid in sorted(threads[r["event_id"]], key=lambda i: when(by_id[i])):
            add_plain(by_id[cid], 1)

    (out_dir / "room_log.txt").write_text(
        "\n".join(plain_lines) + "\n", encoding="utf-8"
    )

    # 6. write HTML ------------------------------------------------------
    html_lines = [
        "<!doctype html><meta charset=utf-8>",
        f"<title>{html.escape(title)} – archive</title>",
        "<style>",
        "body{font:14px/1.45 ui-monospace,monospace;background:#111;color:#eee;padding:1em}",
        ".msg{white-space:pre-wrap}",
        "time{color:#888;margin-right:.5em}",
        ".u{font-weight:600}",
        ".reply{margin-left:2ch}",
        "a{color:#9cf;text-decoration:none}",
        "</style>",
        f"<h1>{html.escape(title)}</h1>",
        "<p><a href='room_log.txt'>⇩ download plaintext</a></p>",
        "<hr>",
    ]

    def add_html(ev, lvl):
        ts = when(ev).strftime("%Y‑%m‑%d %H:%M")
        usr = nice_user(ev["sender"])
        body = html.escape(ev["content"].get("body", ""))
        cls = "msg reply" if lvl else "msg"
        html_lines.append(
            f"<div class='{cls}'>"
            f"<time>{ts}</time>&ensp;"
            f"<span class='u' style='color:{pastel(ev['sender'])}'>{usr}</span>: "
            f"{body}</div>"
        )

    for r in roots:
        add_html(r, 0)
        for cid in sorted(threads[r["event_id"]], key=lambda i: when(by_id[i])):
            add_html(by_id[cid], 1)

    (out_dir / "index.html").write_text(
        "\n".join(html_lines) + "\n", encoding="utf-8"
    )

    logging.info("  ✓ written to %s", out_dir)

# ─── archive all requested rooms ────────────────────────────────────────
for room in ROOMS:
    if room:
        try:
            archive_room(room)
        except Exception as exc:
            logging.error("‼ failed for %s – %s", room, exc)

# ─── master index page --------------------------------------------------
index_lines = [
    "<!doctype html><meta charset=utf-8>",
    "<title>Matrix room archives</title>",
    "<h1>Matrix room archives</h1>",
    "<ul>",
]
for sub in sorted(p.name for p in DST_DIR.iterdir() if p.is_dir()):
    index_lines.append(f"<li><a href='{sub}/'>{html.escape(sub)}</a></li>")
index_lines.append("</ul>")
(DST_DIR / "index.html").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

