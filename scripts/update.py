#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archive one-or-many public Matrix rooms.

Creates
    archive/<slug>/{index.html, room_log.txt}
and a root index.html listing all rooms by their human titles.
"""

# ── std-lib ────────────────────────────────────────────────────────────
import os, sys, json, subprocess, shlex, hashlib, colorsys, logging, re, html
import collections, pathlib, urllib.parse
from   datetime   import datetime, timezone

# ═════════════════════════════════  CONFIG  ════════════════════════════
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

# ══════════  matrix-commander creds  ═══════════════════════════════════
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

# ═══════════════════════  tiny helpers  ═══════════════════════════════
def run(cmd, timeout=None) -> str:
    res = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if res.returncode:
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res.stdout

def json_lines(blob:str):
    for ln in blob.splitlines():
        if ln and ln[0] in "{[":
            try: yield json.loads(ln)
            except json.JSONDecodeError: pass

when      = lambda e: datetime.utcfromtimestamp(e["origin_server_ts"]/1000)
nice_user = lambda u: u.lstrip("@").split(":",1)[0]
slug      = lambda s: urllib.parse.quote(s, safe="").replace("%","_")

def rich_color(uid:str)->str:
    d = hashlib.sha1(uid.encode()).digest()
    h,l,s = int.from_bytes(d[:2],"big")/65535, .55+(d[2]/255-.5)*.25, .55+(d[3]/255-.5)*.25
    r,g,b = colorsys.hls_to_rgb(h,l,s)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

# ── markdown-ish filters ──────────────────────────────────────────────
_re_mdlink = re.compile(r'\[([^\]]+?)\]\((https?://[^\s)]+)\)')
_re_rawurl = re.compile(r'(?<!["\'>])(https?://[^\s<]+)')
_re_fence  = re.compile(r'```(\w+)?\n([\s\S]*?)```', re.MULTILINE)
_re_inline = re.compile(r'`([^`\n]+?)`')
_re_italic = re.compile(r'(?<!\S)\*([^*\n]+?)\*(?!\S)')   # *foo* only

def md_links(t:str)->str:
    t = _re_mdlink.sub(lambda m:
        f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', t)
    return _re_rawurl.sub(lambda m:
        f'<a href="{m.group(0)}" target="_blank" rel="noopener">{m.group(0)}</a>', t)

def fmt_body(body:str)->str:
    segs, pos = [], 0
    for fence in _re_fence.finditer(body):
        segs.append(("txt", body[pos:fence.start()]))
        segs.append(("code", fence))
        pos = fence.end()
    segs.append(("txt", body[pos:]))

    html_out=[]
    for typ,part in segs:
        if typ=="code":
            lang = part.group(1) or ""
            code = html.escape(part.group(2))
            html_out.append(f"<pre><code class='{lang}'>{code}</code></pre>")
            continue
        chunk = html.escape(part)
        chunk = _re_inline.sub(lambda m: f"<code>{html.escape(m.group(1))}</code>", chunk)
        chunk = _re_italic.sub(r"<em>\1</em>", chunk)
        html_out.append(md_links(chunk))
    return "".join(html_out)

# ════════════════════════  archiver  ══════════════════════════════════
def archive(room:str):
    logging.info("room %s", room)

    cfg=json.loads(cred_file.read_text()); cfg.update(room_id=room,default_room=room)
    cred_file.write_text(json.dumps(cfg))

    rdir = pathlib.Path("archive")/slug(room)
    rdir.mkdir(parents=True, exist_ok=True)

    for cmd in (["--room-join",room], ["--room",room,"--listen","once"]):
        try: run(["matrix-commander",*CRED,*cmd])
        except subprocess.CalledProcessError: pass

    title=room
    try:
        info=next(json_lines(run(["matrix-commander",*CRED,"--room",room,
                                  "--get-room-info","--output","json"])),{})
        for k in ("room_display_name","room_name","canonical_alias","room_alias"):
            if info.get(k): title=info[k]; break
    except Exception: pass

    listen={"all":["all"],"tail":["tail","--tail",TAIL_N],"once":["once"]}[LISTEN_MODE]
    raw=run(["matrix-commander",*CRED,"--room",room,"--listen",*listen,"--listen-self","--output","json"],
            timeout=TIMEOUT_S if LISTEN_MODE=="all" else None)

    originals, edits = {}, {}
    for j in json_lines(raw):
        ev = j.get("source", j)
        if ev.get("type")!="m.room.message": continue
        rel=ev["content"].get("m.relates_to",{})
        if rel.get("rel_type")=="m.replace" or "m.new_content" in ev["content"]:
            edits[rel.get("event_id")] = ev
        else:
            originals[ev["event_id"]] = ev

    for eid,msg in originals.items():
        if eid in edits:
            rep=edits[eid]
            new_body = rep["content"].get("m.new_content",{}).get("body") \
                    or rep["content"].get("body","")
            msg["content"]["body"]=new_body
            msg["_edited"]=True

    events=sorted(originals.values(), key=when)
    if not events: return None

    # threading
    byid,threads={e["event_id"]:e for e in events},collections.defaultdict(list)
    for e in events:
        rel=e["content"].get("m.relates_to",{})
        if rel.get("rel_type")=="m.thread":
            threads[rel["event_id"]].append(e["event_id"])
    roots=[e for e in events if e["event_id"] not in {c for kids in threads.values() for c in kids}]

    # plain-text
    stamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    plain=[f"# room: {title}",f"# exported: {stamp}"]
    def pl(ev,lvl):
        body=ev["content"].get("body","")
        if ev.get("_edited"): body+=" [edited]"
        plain.append(f"{'  '*lvl}{'↳ ' if lvl else ''}{when(ev).strftime('%Y-%m-%d %H:%M')} "
                     f"{nice_user(ev['sender'])}: {body}")
    for r in roots:
        pl(r,0)
        for cid in threads[r["event_id"]]: pl(byid[cid],1)

    # html
    last=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    style=f"""
<style>
body{{margin:0 auto;max-width:75ch;font:15px/1.55 system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
     background:#141414;color:#e6e6e6;padding:2rem}}
@media(max-width:480px){{body{{padding:1rem;font-size:14px}} pre{{font-size:13px}}}}
.msg{{white-space:pre-wrap;margin:.3em 0}}
.reply{{margin-left:2ch}}
.edited{{opacity:.65;font-style:italic;font-size:.9em}}
pre{{background:#1e1e1e;padding:.6em;border-radius:4px;overflow:auto}}
code{{font-family:ui-monospace,monospace}}
.u{{font-weight:600}}
.ts,a.ts{{color:#888;text-decoration:none}}
a.ts:hover{{color:#ccc}}
em{{font-style:italic}}
</style>"""
    html_lines=[
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>{html.escape(title)} – archive</title>", style,
        f"<h1>{html.escape(title)}</h1>",
        f"<p><small>last updated {last}</small></p>",
        "<p><a href='room_log.txt'>⇩ plaintext</a> · <a href='../../'>⇦ all rooms</a></p>",
        "<hr>"
    ]
    def add(ev,lvl):
        cls="msg"+(" reply" if lvl else "")
        body=fmt_body(ev['content'].get('body',''))
        if ev.get("_edited"): body+=' <span class="edited">(edited)</span>'
        eid=ev['event_id']
        ts_link = f"<a class='ts' href='https://matrix.to/#/{room}/{eid}' target='_blank'>" \
                  f"{when(ev).strftime('%Y-%m-%d %H:%M')}</a>"
        html_lines.append(f"<div class='{cls}'>"
                          f"{ts_link}&ensp;"
                          f"<span class='u' style='color:{rich_color(ev['sender'])}'>"
                          f"{nice_user(ev['sender'])}</span>: {body}</div>")
    for r in roots:
        add(r,0)
        for cid in threads[r["event_id"]]: add(byid[cid],1)

    (rdir/"room_log.txt").write_text("\n".join(plain)+"\n",encoding="utf-8")
    (rdir/"index.html").write_text("\n".join(html_lines)+"\n",encoding="utf-8")
    logging.info("  wrote → %s", rdir)
    return title, room, slug(room)

# ═════════════════════════════  main  ═════════════════════════════════
pathlib.Path("archive").mkdir(exist_ok=True)
(pathlib.Path("archive/index.html")).unlink(missing_ok=True)

meta=[]
for rid in ROOMS:
    try:
        m=archive(rid)
        if m: meta.append(m)
    except Exception as exc:
        logging.error("‼ failed for %s – %s", rid, exc)

meta.sort(key=lambda t:t[0].lower())
listing="\n".join(
    f"<li><a href='archive/{s}/index.html'>{html.escape(t)}</a>"
    f"<br><small>{html.escape(r)}</small></li>"
    for t,r,s in meta)

landing=f"""<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Archived rooms</title>
<style>
body{{margin:0 auto;max-width:65ch;font:16px/1.55 system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
     background:#141414;color:#e6e6e6;padding:2rem}}
a{{color:#9cf;text-decoration:none}}
@media(max-width:480px){{body{{padding:1rem;font-size:15px}}}}
</style>
<h1>Archived rooms</h1><ul>{listing}</ul>"""
pathlib.Path("index.html").write_text(landing, encoding="utf-8")
logging.info("root index.html regenerated ✓")

