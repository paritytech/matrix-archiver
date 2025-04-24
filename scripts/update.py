#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive one-or-many public Matrix rooms.

Creates
    archive/<slug>/{index.html, room_log.txt}
and a root index.html listing all rooms by their human titles.
"""

# ───── std-lib ────────────────────────────────────────────────────────
import os, sys, json, subprocess, shlex, hashlib, colorsys, logging, re, html
import collections, pathlib, urllib.parse
from datetime import datetime, timezone

# ══════════════════════════════════════════════════════════════════════
# ░░  CONFIG  ░░
# ══════════════════════════════════════════════════════════════════════
HS        = os.environ["MATRIX_HS"]
USER_ID   = os.environ["MATRIX_USER"]
TOKEN     = os.environ["MATRIX_TOKEN"]

ROOMS_RAW = os.getenv("MATRIX_ROOMS") or os.getenv("MATRIX_ROOM", "")
ROOMS     = [r.strip() for r in re.split(r"[,\s]+", ROOMS_RAW) if r.strip()]
if not ROOMS:
    sys.exit("‼  No MATRIX_ROOMS specified")

LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()        # all|tail|once
TAIL_N      = os.getenv("TAIL_N", "10000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", 20))

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s: %(message)s",
                    stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"

# ══════════════════════════════════════════════════════════════════════
# ░░  MATRIX-COMMANDER CREDENTIALS  ░░
# ══════════════════════════════════════════════════════════════════════
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)

if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver"  : HS,
        "user_id"     : USER_ID,
        "access_token": TOKEN,
        "device_id"   : "GH",
        "default_room": ROOMS[0],
    }))

CRED = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ══════════════════════════════════════════════════════════════════════
# ░░  HELPERS  ░░
# ══════════════════════════════════════════════════════════════════════
def run(cmd, timeout=None) -> str:
    if logging.getLogger().level <= logging.DEBUG:
        logging.debug("⟹ %s", " ".join(map(shlex.quote, cmd)))
    res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if logging.getLogger().level <= logging.DEBUG:
        for l in res.stderr.splitlines(): logging.debug(l)
    if res.returncode:
        raise subprocess.CalledProcessError(res.returncode, cmd,
                                            res.stdout, res.stderr)
    return res.stdout

def json_lines(blob: str):
    for line in blob.splitlines():
        line = line.strip()
        if line and line[0] in "{[":
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass

when        = lambda e: datetime.utcfromtimestamp(e["origin_server_ts"]/1000)
nice_user   = lambda u: u.lstrip("@").split(":", 1)[0]
slug        = lambda s: urllib.parse.quote(s, safe="").replace("%", "_")

# ── colour hashing ────────────────────────────────────────────────────
def rich_color(uid: str) -> str:
    digest = hashlib.sha1(uid.encode()).digest()
    hue        = int.from_bytes(digest[:2], "big") / 0xFFFF
    lightness  = 0.55 + (digest[2]/255 - .5) * 0.25
    saturation = 0.55 + (digest[3]/255 - .5) * 0.25
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

# ── regexp helpers (links, code) ──────────────────────────────────────
_re_mdlink  = re.compile(r'\[([^\]]+?)\]\((https?://[^\s)]+)\)')
_re_rawurl  = re.compile(r'(?<!["\'>])(https?://[^\s<]+)')
_re_fence   = re.compile(r'```(\w+)?\n([\s\S]*?)```', re.MULTILINE)
_re_inline  = re.compile(r'`([^`\n]+?)`')

def md_links(text: str) -> str:
    text = _re_mdlink.sub(lambda m:
        f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', text)
    text = _re_rawurl.sub(lambda m:
        f'<a href="{m.group(0)}" target="_blank" rel="noopener">{m.group(0)}</a>', text)
    return text

def format_body(body: str) -> str:
    """HTML-escape + linkify + highlight code blocks / inline code."""
    parts, pos = [], 0
    for fence in _re_fence.finditer(body):
        parts.append(("text", body[pos:fence.start()]))
        parts.append(("fence", fence))
        pos = fence.end()
    parts.append(("text", body[pos:]))

    out = []
    for kind, data in parts:
        if kind == "fence":
            lang = data.group(1) or ""
            code = html.escape(data.group(2))
            out.append(f'<pre><code class="{lang}">{code}</code></pre>')
            continue

        segment = data
        p = 0
        for ic in _re_inline.finditer(segment):
            out.append(md_links(html.escape(segment[p:ic.start()])))
            out.append(f"<code>{html.escape(ic.group(1))}</code>")
            p = ic.end()
        out.append(md_links(html.escape(segment[p:])))
    return "".join(out)

# ══════════════════════════════════════════════════════════════════════
# ░░  ARCHIVER  ░░
# ══════════════════════════════════════════════════════════════════════
def archive_room(room: str):
    logging.info("room: %s", room)

    # update creds so commander targets *this* room for login shortcuts
    cred = json.loads(cred_file.read_text())
    cred["room_id"] = cred["default_room"] = room
    cred_file.write_text(json.dumps(cred))

    room_dir = pathlib.Path("archive") / slug(room)
    room_dir.mkdir(parents=True, exist_ok=True)

    # join & quick sync (idempotent)
    try: run(["matrix-commander", *CRED, "--room-join", room])
    except subprocess.CalledProcessError: pass
    try: run(["matrix-commander", *CRED, "--room", room, "--listen", "once"])
    except subprocess.CalledProcessError: pass

    # human-friendly room title
    title = room
    try:
        info = next(json_lines(run(["matrix-commander", *CRED,
                                    "--room", room, "--get-room-info",
                                    "--output", "json"])), {})
        for k in ("room_display_name", "room_name",
                  "canonical_alias", "room_alias"):
            if info.get(k): title = info[k]; break
    except Exception as e:
        logging.warning("  get-room-info failed – %s", e)

    # ── fetch messages ────────────────────────────────────────────────
    listen = {
        "all":  ["--listen", "all",  "--listen-self"],
        "tail": ["--listen", "tail", "--tail", TAIL_N, "--listen-self"],
        "once": ["--listen", "once", "--listen-self"],
    }[LISTEN_MODE]

    blob = run(["matrix-commander", *CRED, "--room", room,
                *listen, "--output", "json"],
               timeout=TIMEOUT_S if LISTEN_MODE == "all" else None)

    # ── originals + latest edits ──────────────────────────────────────
    originals: dict[str, dict] = {}
    edits:     dict[str, dict] = {}          # key = original-event-id

    for j in json_lines(blob):
        ev = j.get("source", j)
        if ev.get("type") != "m.room.message":
            continue

        c   = ev["content"]
        rel = c.get("m.relates_to", {})
        is_edit = (
            rel.get("rel_type") == "m.replace"
            or "m.new_content" in c
        )

        if is_edit:
            # replace keeps the *latest* only (newer overwrite older)
            edits[rel.get("event_id")] = ev
        else:
            originals[ev["event_id"]] = ev

    # merge edits onto originals
    for eid, base in originals.items():
        if eid in edits:
            rep = edits[eid]
            new_body = (rep["content"]
                        .get("m.new_content", {})
                        .get("body")
                        or rep["content"].get("body", ""))
            base["content"]["body"] = new_body
            base["_edited"] = True

    events = list(originals.values())
    logging.info("  messages (after de-dupe): %d", len(events))
    if not events:
        return (room, title, slug(room))

    # ── threading split ───────────────────────────────────────────────
    by_id   = {e["event_id"]: e for e in events}
    threads = collections.defaultdict(list)
    for e in events:
        rel = e["content"].get("m.relates_to", {})
        if rel.get("rel_type") == "m.thread":
            threads[rel["event_id"]].append(e["event_id"])
    roots = sorted(
        [e for e in events if e["event_id"] not in
         {c for kids in threads.values() for c in kids}],
        key=when)

    # ── plain-text log ────────────────────────────────────────────────
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    txt   = [f"# room: {title}", f"# exported: {stamp}"]

    def add_txt(ev, lvl):
        arrow = "↳ " if lvl else ""
        body  = ev["content"].get("body", "")
        if ev.get("_edited"): body += " [edited]"
        txt.append(f"{'  '*lvl}{arrow}{when(ev).strftime('%Y-%m-%d %H:%M')} "
                   f"{nice_user(ev['sender'])}: {body}")

    for root in roots:
        add_txt(root, 0)
        for cid in sorted(threads[root["event_id"]], key=lambda c: when(by_id[c])):
            add_txt(by_id[cid], 1)

    # ── HTML view ─────────────────────────────────────────────────────
    last_updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html_lines = [
        "<!doctype html><meta charset=utf-8>",
        f"<title>{html.escape(title)} – archive</title>",
        "<style>",
        "body{margin:0 auto;max-width:75ch;font:15px/1.55 system-ui,"
        "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;background:#141414;color:#e6e6e6;padding:2rem}",
        ".msg{white-space:pre-wrap;margin:0.3em 0}",
        ".edited{opacity:0.75;font-style:italic}",
        ".reply{margin-left:2ch}",
        "pre{background:#1e1e1e;padding:0.6em;border-radius:4px;overflow:auto}",
        "code{font-family:ui-monospace,monospace}",
        ".u{font-weight:600}",
        "time{color:#888}",
        "a{color:#9cf;text-decoration:none}",
        "</style>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p><small>last updated {last_updated}</small></p>",
        "<p><a href='room_log.txt'>⇩ plaintext</a>  ·  <a href='../../'>⇦ all rooms</a></p>",
        "<hr>",
    ]

    def add_html(ev, lvl):
        cls = "msg" + (" reply" if lvl else "") + (" edited" if ev.get("_edited") else "")
        html_lines.append(
            f"<div class='{cls}'>"
            f"<time>{when(ev).strftime('%Y-%m-%d %H:%M')}</time>&ensp;"
            f"<span class='u' style='color:{rich_color(ev['sender'])}'>"
            f"{nice_user(ev['sender'])}</span>: "
            f"{format_body(ev['content'].get('body',''))}</div>")

    for root in roots:
        add_html(root, 0)
        for cid in sorted(threads[root["event_id"]], key=lambda c: when(by_id[c])):
            add_html(by_id[cid], 1)

    # ── write artefacts ───────────────────────────────────────────────
    (room_dir / "room_log.txt").write_text("\n".join(txt) + "\n", encoding="utf-8")
    (room_dir / "index.html").write_text("\n".join(html_lines) + "\n", encoding="utf-8")
    logging.info("  written → %s", room_dir)

    return (room, title, slug(room))

# ══════════════════════════════════════════════════════════════════════
# ░░  MAIN  ░░
# ══════════════════════════════════════════════════════════════════════
pathlib.Path("archive").mkdir(exist_ok=True)
(pathlib.Path("archive/index.html")).unlink(missing_ok=True)

room_meta = []
for rid in ROOMS:
    try:
        meta = archive_room(rid)
        if meta:
            room_meta.append(meta)
    except Exception as exc:
        logging.error("‼ failed for %s – %s", rid, exc)

room_meta.sort(key=lambda t: t[1].lower())     # sort by title

items = "\n".join(
    f"<li><a href='archive/{slg}/index.html'>{html.escape(title)}</a>"
    f"<br><small>{html.escape(rid)}</small></li>"
    for rid, title, slg in room_meta)

pathlib.Path("index.html").write_text(
    "\n".join([
        "<!doctype html><meta charset=utf-8>",
        "<title>Archived rooms</title>",
        "<style>",
        "body{margin:0 auto;max-width:65ch;font:16px/1.55 system-ui,"
        "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;background:#141414;color:#e6e6e6;padding:2rem}",
        "a{color:#9cf;text-decoration:none}",
        "</style>",
        "<h1>Archived rooms</h1>",
        "<ul>", items, "</ul>"
    ]) + "\n",
    encoding="utf-8")

logging.info("root index.html regenerated ✓")

