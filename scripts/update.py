#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive one-or-many public Matrix rooms.
Creates archive/<slug>/{index.html,room_log.txt}
and a root index.html listing all rooms by their human title.
"""

import os, sys, json, subprocess, shlex, hashlib, colorsys, logging, re, html
import collections, pathlib, urllib.parse
from datetime import datetime, timezone

# ─── config ───────────────────────────────────────────────────────────
HS, USER_ID, TOKEN = map(os.environ.get, ("MATRIX_HS", "MATRIX_USER", "MATRIX_TOKEN"))
ROOMS_RAW = os.getenv("MATRIX_ROOMS") or os.getenv("MATRIX_ROOM", "")
ROOMS = [r for r in re.split(r"[,\s]+", ROOMS_RAW) if r]
if not ROOMS:
    sys.exit("‼  MATRIX_ROOMS is empty")

LISTEN_MODE = os.getenv("LISTEN_MODE", "all").lower()        # all|tail|once
TAIL_N      = os.getenv("TAIL_N", "10000")
TIMEOUT_S   = int(os.getenv("TIMEOUT", 20))

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s: %(message)s", stream=sys.stderr)
os.environ["NIO_LOG_LEVEL"] = "error"

# ─── commander credentials & store ────────────────────────────────────
cred_file = pathlib.Path("mc_creds.json")
store_dir = pathlib.Path("store"); store_dir.mkdir(exist_ok=True)
if not cred_file.exists():
    cred_file.write_text(json.dumps({
        "homeserver": HS, "user_id": USER_ID, "access_token": TOKEN,
        "device_id": "GH", "default_room": ROOMS[0],
    }))
CRED = ["--credentials", str(cred_file), "--store", str(store_dir)]

# ─── util helpers ─────────────────────────────────────────────────────
run = lambda c, **kw: subprocess.run(c, text=True, capture_output=True, **kw).stdout
json_lines = lambda b: (json.loads(l) for l in b.splitlines() if l.strip().startswith(("{","[")))
when   = lambda e: datetime.utcfromtimestamp(e["origin_server_ts"]/1000)
user   = lambda u: u.lstrip("@").split(":",1)[0]
slug   = lambda s: urllib.parse.quote(s, safe="").replace("%","_")
color  = lambda uid: "#{:02x}{:02x}{:02x}".format(*[
            int(c*255) for c in colorsys.hls_to_rgb(
                int(hashlib.sha1(uid.encode()).hexdigest()[:8],16)/0xffffffff,
                .6,.55)])

# markdown → html helpers (links, code-spans/fences) – unchanged
_re_mdlink  = re.compile(r'\[([^\]]+?)\]\((https?://[^\s)]+)\)')
_re_rawurl  = re.compile(r'(?<!["\'>])(https?://[^\s<]+)')
_re_fence   = re.compile(r'```(\w+)?\n([\s\S]*?)```', re.MULTILINE)
_re_inline  = re.compile(r'`([^`\n]+?)`')
def md_links(t:str)->str:
    t=_re_mdlink.sub(lambda m:f'<a href="{m[2]}" target="_blank">{m[1]}</a>',t)
    return _re_rawurl.sub(lambda m:f'<a href="{m[0]}" target="_blank">{m[0]}</a>',t)
def format_body(b:str)->str:
    parts,pos=[],0
    for f in _re_fence.finditer(b):
        parts+=["text",b[pos:f.start()],("fence",f)];pos=f.end()
    parts+=["text",b[pos:]]
    out=[]
    it=iter(parts)
    for kind,seg in zip(it,it):
        if kind=="text":
            p=0
            for ic in _re_inline.finditer(seg):
                out+= [md_links(html.escape(seg[p:ic.start()])),
                       f"<code>{html.escape(ic[1])}</code>"]; p=ic.end()
            out.append(md_links(html.escape(seg[p:])))
        else:
            lang=seg[0][1] or ""
            code=html.escape(seg[0][2])
            out.append(f'<pre><code class="{lang}">{code}</code></pre>')
    return "".join(out)

# ─── archiver for one room ────────────────────────────────────────────
def archive(room:str):
    logging.info("room: %s", room)
    cred=json.loads(cred_file.read_text()); cred|={"room_id":room,"default_room":room}
    cred_file.write_text(json.dumps(cred))

    run(["matrix-commander",*CRED,"--room-join",room])
    run(["matrix-commander",*CRED,"--room",room,"--listen","once"])

    # title
    title=room
    try:
        info=next(json_lines(run(["matrix-commander",*CRED,"--room",room,
                                  "--get-room-info","--output","json"])))
        for k in ("room_display_name","room_name","canonical_alias","room_alias"):
            if info.get(k): title=info[k]; break
    except: pass

    # fetch
    listen={
        "all":["--listen","all","--listen-self"],
        "tail":["--listen","tail","--tail",TAIL_N,"--listen-self"],
        "once":["--listen","once","--listen-self"]}[LISTEN_MODE]
    blob=run(["matrix-commander",*CRED,"--room",room,*listen,"--output","json"],
             timeout=TIMEOUT_S if LISTEN_MODE=="all" else None)

    originals, edits = {}, {}
    for j in json_lines(blob):
        ev=j.get("source",j)
        if ev.get("type")!="m.room.message": continue
        rel=ev["content"].get("m.relates_to",{})
        if rel.get("rel_type")=="m.replace":
            edits[rel["event_id"]] = ev
        else:
            originals[ev["event_id"]] = ev

    # apply edits & mark
    for eid,base in originals.items():
        if eid in edits:
            new = edits[eid]["content"].get("m.new_content") or edits[eid]["content"]
            base["content"]["body"] = new.get("body","")
            base["_edited"]=True

    events=list(originals.values())
    if not events: return (room,title,slug(room))
    logging.info("  messages: %d",len(events))

    # threading
    threads=collections.defaultdict(list)
    for e in events:
        rel=e["content"].get("m.relates_to",{})
        if rel.get("rel_type")=="m.thread":
            threads[rel["event_id"]].append(e["event_id"])
    by_id={e["event_id"]:e for e in events}
    roots=sorted([e for e in events if e["event_id"] not in
                 {c for kids in threads.values() for c in kids}], key=when)

    # plaintext
    stamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    txt=[f"# room: {title}",f"# exported: {stamp}"]
    def add_txt(ev,lvl):
        txt.append(f"{'  '*lvl}{'↳ ' if lvl else ''}"
                   f"{when(ev).strftime('%Y-%m-%d %H:%M')} "
                   f"{user(ev['sender'])}: {ev['content'].get('body','')}"
                   f"{' [edited]' if ev.get('_edited') else ''}")
    for r in roots:
        add_txt(r,0)
        for cid in sorted(threads[r["event_id"]], key=lambda c: when(by_id[c])):
            add_txt(by_id[cid],1)

    # html
    room_dir=pathlib.Path("archive")/slug(room); room_dir.mkdir(parents=True,exist_ok=True)
    last=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html_lines=[
        "<!doctype html><meta charset=utf-8>",
        f"<title>{html.escape(title)} – archive</title>",
        "<style>",
        "body{margin:auto;max-width:75ch;font:15px/1.6 system-ui,-apple-system,"
        "'Segoe UI',Helvetica,Arial,sans-serif;background:#141414;color:#e6e6e6;padding:2rem}",
        ".msg{margin:.3em 0;white-space:pre-wrap}",
        ".reply{margin-left:2ch}",
        ".edited::after{content:' (edited)';opacity:.6;font-style:italic}",
        "time{color:#888;margin-right:.5em}",
        ".u{font-weight:600}",
        "pre{background:#1e1e1e;padding:.6em;border-radius:4px;overflow:auto}",
        "code{font-family:ui-monospace,monospace}",
        "a{color:#9cf;text-decoration:none}",
        "</style>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p><small>last updated {last}</small></p>",
        "<p><a href='room_log.txt'>⇩ plaintext</a>  ·  "
        "<a href='../../'>⇦ all rooms</a></p>",
        "<hr>"
    ]
    def add_html(ev,lvl):
        cls="msg"+(" reply" if lvl else "")+(" edited" if ev.get('_edited') else "")
        html_lines.append(
            f"<div class='{cls}'>"
            f"<time>{when(ev).strftime('%Y-%m-%d %H:%M')}</time>"
            f"<span class='u' style='color:{color(ev['sender'])}'>"
            f"{user(ev['sender'])}</span>: "
            f"{format_body(ev['content'].get('body',''))}</div>")
    for r in roots:
        add_html(r,0)
        for cid in sorted(threads[r["event_id"]], key=lambda c: when(by_id[c])):
            add_html(by_id[cid],1)

    (room_dir/'room_log.txt').write_text("\n".join(txt)+"\n",encoding='utf-8')
    (room_dir/'index.html' ).write_text("\n".join(html_lines)+"\n",encoding='utf-8')
    logging.info("  written → %s", room_dir)
    return (room,title,slug(room))

# ─── main loop ────────────────────────────────────────────────────────
pathlib.Path("archive").mkdir(exist_ok=True)
(pathlib.Path("archive/index.html")).unlink(missing_ok=True)

meta=[]
for r in ROOMS:
    try:
        m=archive(r)
        if m: meta.append(m)
    except Exception as e:
        logging.error("‼ %s – %s",r,e)

meta.sort(key=lambda t:t[1].lower())
items="\n".join(f"<li><a href='archive/{s}/index.html'>{html.escape(t)}</a>"
                f"<br><small>{html.escape(r)}</small></li>" for r,t,s in meta)

pathlib.Path("index.html").write_text(
    "\n".join([
        "<!doctype html><meta charset=utf-8>",
        "<title>Archived rooms</title>",
        "<style>body{margin:auto;max-width:65ch;font:16px/1.6 system-ui,-apple-system,"
        "'Segoe UI',Helvetica,Arial,sans-serif;background:#141414;color:#e6e6e6;padding:2rem}"
        "a{color:#9cf;text-decoration:none}</style>",
        "<h1>Archived rooms</h1>",
        "<ul>",items,"</ul>"
    ])+"\n",encoding='utf-8')

logging.info("root index.html regenerated ✓")

