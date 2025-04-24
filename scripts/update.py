#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive one-or-many public Matrix rooms.
Creates archive/<slug>/{index.html,room_log.txt}
plus a root index.html listing every room.
"""

# ─── std-lib ───────────────────────────────────────────────────────────
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
ROOMS     = [r for r in re.split(r"[,\s]+", ROOMS_RAW) if r]
if not ROOMS:
    sys.exit("‼  No MATRIX_ROOMS specified")

LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()      # all|tail|once
TAIL_N      = os.getenv("TAIL_N", "10000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", 20))

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s: %(message)s", stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"

# ══════════════════════════════════════════════════════════════════════
# ░░  matrix-commander credentials  ░░
# ══════════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════════
# ░░  helpers  ░░
# ══════════════════════════════════════════════════════════════════════
def run(cmd, timeout=None) -> str:
    res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if res.returncode:
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res.stdout

def json_lines(blob: str):
    for ln in blob.splitlines():
        if ln and ln[0] in "{[":
            try:
                yield json.loads(ln)
            except json.JSONDecodeError:
                pass

when      = lambda e: datetime.utcfromtimestamp(e["origin_server_ts"]/1000)
nice_user = lambda u: u.lstrip("@").split(":", 1)[0]
slug      = lambda s: urllib.parse.quote(s, safe="").replace("%", "_")

# ── colour hashing (good distribution) ────────────────────────────────
def rich_color(uid: str) -> str:
    d = hashlib.sha1(uid.encode()).digest()
    h,l,s = int.from_bytes(d[:2], "big")/0xFFFF, .55+(d[2]/255-.5)*.25, .55+(d[3]/255-.5)*.25
    r,g,b = colorsys.hls_to_rgb(h,l,s)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

# ── markdown-ish helpers ──────────────────────────────────────────────
_re_mdlink = re.compile(r'\[([^\]]+?)\]\((https?://[^\s)]+)\)')
_re_rawurl = re.compile(r'(?<!["\'>])(https?://[^\s<]+)')
_re_fence  = re.compile(r'```(\w+)?\n([\s\S]*?)```', re.MULTILINE)
_re_inline = re.compile(r'`([^`\n]+?)`')
_re_italic = re.compile(r'(?<!\S)\*([^*\n]+?)\*(?!\S)')   # new stricter rule

def md_links(t: str) -> str:
    t = _re_mdlink.sub(lambda m:
        f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', t)
    return _re_rawurl.sub(lambda m:
        f'<a href="{m.group(0)}" target="_blank" rel="noopener">{m.group(0)}</a>', t)

def fmt_body(body: str) -> str:
    # Fenced blocks first
    out, pos = [], 0
    for fence in _re_fence.finditer(body):
        out.append(_re_italic.sub(r'<em>\1</em>', md_links(
                   html.escape(body[pos:fence.start()]))))
        lang = fence.group(1) or ""
        code = html.escape(fence.group(2))
        out.append(f"<pre><code class='{lang}'>{code}</code></pre>")
        pos = fence.end()
    tail = html.escape(body[pos:])

    seg, p = [], 0
    for ic in _re_inline.finditer(tail):
        seg.append(_re_italic.sub(r'<em>\1</em>', md_links(tail[p:ic.start()])))
        seg.append(f"<code>{html.escape(ic.group(1))}</code>")
        p = ic.end()
    seg.append(_re_italic.sub(r'<em>\1</em>', md_links(tail[p:])))
    out.append("".join(seg))
    return "".join(out)

# ══════════════════════════════════════════════════════════════════════
# ░░  archiver  ░░
# ══════════════════════════════════════════════════════════════════════
def archive(room: str) -> tuple[str, str, str] | None:
    logging.info("room %s", room)

    # keep commander happy
    cfg = json.loads(cred_file.read_text()); cfg.update(room_id=room, default_room=room)
    cred_file.write_text(json.dumps(cfg))

    rdir = pathlib.Path("archive") / slug(room)
    rdir.mkdir(parents=True, exist_ok=True)

    for cmd in (["--room-join", room],
                ["--room", room, "--listen", "once"]):
        try: run(["matrix-commander", *CRED, *cmd])
        except subprocess.CalledProcessError:
            pass

    title = room
    try:
        info = next(json_lines(run(["matrix-commander", *CRED,
                                    "--room", room, "--get-room-info",
                                    "--output", "json"])), {})
        for k in ("room_display_name", "room_name",
                  "canonical_alias", "room_alias"):
            if info.get(k):
                title = info[k]; break
    except Exception:  # fine – room alias will do
        pass

    listen_args = {
        "all" : ["--listen", "all",  "--listen-self"],
        "tail": ["--listen", "tail", "--tail", TAIL_N, "--listen-self"],
        "once": ["--listen", "once", "--listen-self"],
    }[LISTEN_MODE]

    stream = run(["matrix-commander", *CRED, "--room", room,
                  *listen_args, "--output", "json"],
                 timeout=TIMEOUT_S if LISTEN_MODE == "all" else None)

    originals, edits = {}, {}
    for j in json_lines(stream):
        ev = j.get("source", j)
        if ev.get("type") != "m.room.message":
            continue
        rel = ev["content"].get("m.relates_to", {})
        # replacement?
        if rel.get("rel_type") == "m.replace" or "m.new_content" in ev["content"]:
            edits[rel.get("event_id")] = ev
        else:
            originals[ev["event_id"]] = ev

    # apply latest edit
    for eid, msg in originals.items():
        if eid in edits:
            rep = edits[eid]
            new_body = rep["content"].get("m.new_content", {}).get("body") \
                    or rep["content"].get("body", "")
            msg["content"]["body"] = new_body
            msg["_edited"] = True

    events = sorted(originals.values(), key=when)
    if not events:
        return None

    # threading
    byid = {e["event_id"]: e for e in events}
    threads = collections.defaultdict(list)
    for e in events:
        rel = e["content"].get("m.relates_to", {})
        if rel.get("rel_type") == "m.thread":
            threads[rel["event_id"]].append(e["event_id"])
    roots = [e for e in events if e["event_id"] not in
             {c for kids in threads.values() for c in kids}]

    # plain-text export
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    plain = [f"# room: {title}", f"# exported: {stamp}"]
    def line(ev,lvl):
        txt = ev["content"].get("body", "")
        if ev.get("_edited"): txt += " [edited]"
        plain.append(f"{'  '*lvl}{'↳ ' if lvl else ''}{when(ev).strftime('%Y-%m-%d %H:%M')} "
                     f"{nice_user(ev['sender'])}: {txt}")
    for r in roots:
        line(r,0)
        for cid in threads[r["event_id"]]:
            line(byid[cid],1)

    # html export
    last = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html_lines = [
        "<!doctype html><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>{html.escape(title)} – archive</title>",
        """
<style>
body{
    margin:0 auto;
    max-width:75ch;
    font:15px/1.55 system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
    background:#141414;
    color:#e6e6e6;
    padding:2rem
}
.msg{white-space:pre-wrap;margin:.3em 0}
.reply{margin-left:2ch}
.edited{opacity:.7;font-style:italic}
pre{background:#1e1e1e;padding:.6em;border-radius:4px;overflow:auto}
code{font-family:ui-monospace,monospace}
.u{font-weight:600}
time{color:#888}
a{color:#9cf;text-decoration:none}
i,em{font-style:normal}           /* kill stray italics */
em{font-style:italic}             /* restore our own */
@media(max-width:480px){
    body{padding:1rem;font-size:14px}
    pre{font-size:13px}
}
</style>""",
        f"<h1>{html.escape(title)}</h1>",
        f"<p><small>last updated {last}</small></p>",
        "<p><a href='room_log.txt'>⇩ plaintext</a> · "
        "<a href='../../'>⇦ all rooms</a></p>",
        "<hr>"
    ]
    def add(ev,lvl):
        cls = "msg" + (" reply" if lvl else "") + (" edited" if ev.get("_edited") else "")
        html_lines.append(
            f"<div class='{cls}'>"
            f"<time>{when(ev).strftime('%Y-%m-%d %H:%M')}</time>&ensp;"
            f"<span class='u' style='color:{rich_color(ev['sender'])}'>"
            f"{nice_user(ev['sender'])}</span>: "
            f"{fmt_body(ev['content'].get('body',''))}</div>")
    for r in roots:
        add(r,0)
        for cid in threads[r["event_id"]]:
            add(byid[cid],1)

    # write files
    (rdir/"room_log.txt").write_text("\n".join(plain)+"\n",encoding="utf-8")
    (rdir/"index.html").write_text("\n".join(html_lines)+"\n",encoding="utf-8")
    logging.info("  wrote → %s", rdir)
    return title, room, slug(room)

# ══════════════════════════════════════════════════════════════════════
# ░░  main  ░░
# ══════════════════════════════════════════════════════════════════════
pathlib.Path("archive").mkdir(exist_ok=True)
(pathlib.Path("archive/index.html")).unlink(missing_ok=True)  # old artefact

meta=[]
for r in ROOMS:
    try:
        m=archive(r)
        if m: meta.append(m)
    except Exception as exc:
        logging.error("‼ failed for %s – %s", r, exc)

meta.sort(key=lambda t:t[0].lower())
listing="\n".join(
    f"<li><a href='archive/{s}/index.html'>{html.escape(t)}</a>"
    f"<br><small>{html.escape(r)}</small></li>"
    for t,r,s in meta)

landing = f"""\
<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Archived rooms</title>
<style>
body{{margin:0 auto;max-width:65ch;font:16px/1.55 system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
      background:#141414;color:#e6e6e6;padding:2rem}}
a{{color:#9cf;text-decoration:none}}
</style>
<h1>Archived rooms</h1>
<ul>{listing}</ul>
"""
pathlib.Path("index.html").write_text(landing, encoding="utf-8")
logging.info("root index.html regenerated ✓")

