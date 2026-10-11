#!/usr/bin/env python3
"""yt-ui v3: a local-only front end for yt-dlp on macOS.

Run:   python3 ~/yt-ui/app_v3.py
Needs: brew install yt-dlp ffmpeg
"""
import json, os, re, signal, subprocess, threading, uuid, webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Apps started by double-click don't inherit Homebrew's PATH, so add it.
os.environ["PATH"] = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])

PORT = 8765
DEFAULT_DIR = str(Path.home() / "Downloads")
NOT_INSTALLED = "yt-dlp isn't installed. Run: brew install yt-dlp"

jobs, procs = {}, {}
thumbs = OrderedDict()
lock = threading.Lock()
FORMAT_RE = re.compile(r"^(v:best|v:\d{3,4}|a:mp3|a:m4a)$")
MP4 = ["--merge-output-format", "mp4"]


# ---------------------------------------------------------------- helpers

def format_args(fmt):
    kind, val = fmt.split(":")
    if kind == "a":
        if val == "mp3":
            return ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        return ["-x", "--audio-format", "m4a"]
    if val == "best":
        return ["-S", "vcodec:h264,res,acodec:m4a"] + MP4
    return ["-S", "res:%s,vcodec:h264,acodec:m4a" % val] + MP4


def clean_name(name):
    """Turn a user-typed title into a safe file name (no extension)."""
    name = re.sub(r"[\x00-\x1f/\\:]", "-", str(name)).strip().lstrip(". -").strip()
    name = re.sub(r"\.(mp4|m4a|mp3|webm|mkv)$", "", name, flags=re.I)
    name = name.encode("utf-8")[:180].decode("utf-8", "ignore").strip()
    return name.replace("%", "%%")      # "%" is special in yt-dlp output templates


ERRORS = [
    ("private video", "This video is private."),
    ("confirm your age", "This video is age-restricted and needs a signed-in account."),
    ("not a bot", "YouTube is asking for a bot check. Run: brew upgrade yt-dlp, then try again."),
    ("members-only", "This video is for channel members only."),
    ("join this channel", "This video is for channel members only."),
    ("video unavailable", "This video isn't available."),
    ("unsupported url", "That link isn't supported."),
    ("not available in your country", "This video isn't available in your region."),
    ("in your country", "This video isn't available in your region."),
    ("live event will begin", "This live stream hasn't started yet."),
    ("premieres in", "This premiere hasn't started yet."),
    ("requested format is not available", "That quality isn't available for this video. Pick another one."),
    ("ffmpeg not found", "ffmpeg isn't installed. Run: brew install ffmpeg"),
    ("ffprobe not found", "ffmpeg isn't installed. Run: brew install ffmpeg"),
    ("too many requests", "Too many requests. Wait a few minutes and try again."),
    ("http error 429", "Too many requests. Wait a few minutes and try again."),
    ("permission denied", "Can't write to that folder. Choose another one."),
    ("unable to download webpage", "Can't reach the site. Check your internet connection."),
    ("name or service not known", "Can't reach the site. Check your internet connection."),
    ("nodename nor servname", "Can't reach the site. Check your internet connection."),
    ("network is unreachable", "Can't reach the site. Check your internet connection."),
    ("timed out", "The connection timed out. Try again."),
]


def friendly(text):
    low = text.lower()
    for key, msg in ERRORS:
        if key in low:
            return msg
    lines = [l for l in text.splitlines() if "ERROR:" in l]
    line = (lines[-1] if lines else (text.strip().splitlines() or ["Something went wrong"])[-1])
    return "Download failed: " + line.split("ERROR:", 1)[-1].strip()[:160]


def notify(text):
    try:
        subprocess.run(
            ["osascript", "-e", "on run argv", "-e",
             "display notification (item 1 of argv) with title \"yt\"",
             "-e", "end run", "Saved: " + text],
            capture_output=True, timeout=10)
    except Exception:
        pass


def reveal(job):
    try:
        p = job.get("_path")
        if p and os.path.exists(p):
            subprocess.run(["open", "-R", p], timeout=10)
        else:
            subprocess.run(["open", job["_dir"]], timeout=10)
    except Exception:
        pass


def sniff(b):
    if b[:3] == b"\xff\xd8\xff": return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    if b[:4] == b"GIF8": return "image/gif"
    return None


def grab_thumb(url):
    """Download the thumbnail with curl (uses the macOS certificate store)."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        r = subprocess.run(
            ["curl", "-fsSL", "--max-time", "10", "--max-filesize", "6000000",
             "-A", "Mozilla/5.0", "--url", url],
            capture_output=True, timeout=15)
    except Exception:
        return None
    ctype = sniff(r.stdout) if r.returncode == 0 else None
    if not ctype:
        return None
    tok = uuid.uuid4().hex
    with lock:
        thumbs[tok] = (r.stdout, ctype)
        while len(thumbs) > 24:
            thumbs.popitem(last=False)
    return "/thumb/" + tok


def get_info(url):
    cmd = ["yt-dlp", "-J", "--no-warnings", "--no-playlist", "--playlist-items", "1",
           "--socket-timeout", "15", "--", url]
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except OSError:
        return {"error": NOT_INSTALLED}
    except subprocess.TimeoutExpired:
        return {"error": "That took too long. Check your connection and try again."}
    if r.returncode != 0:
        return {"error": friendly(r.stderr or "Couldn't read that link.")}
    try:
        info = json.loads(r.stdout)
    except ValueError:
        return {"error": "Couldn't read that link."}

    playlist = info.get("_type") == "playlist"
    if playlist:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return {"error": "Nothing to download at that link."}
        info = entries[0]

    best = {}
    for f in info.get("formats") or []:
        vc = f.get("vcodec") or "none"
        h, w = f.get("height"), f.get("width")
        if vc == "none" or not h or f.get("ext") == "mhtml":
            continue
        res = int(min(h, w)) if w else int(h)   # same meaning as yt-dlp's "res"
        best[res] = best.get(res, False) or vc.startswith(("avc1", "h264"))
    heights = [{"h": h, "h264": best[h]} for h in sorted(best, reverse=True) if h >= 144]

    thumb_url = info.get("thumbnail") or ((info.get("thumbnails") or [{}])[-1].get("url"))
    return {
        "ok": True,
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumb": grab_thumb(thumb_url),
        "heights": heights,
        "playlist": playlist,
    }


# ---------------------------------------------------------------- downloads

def run(jid, url, fmt, outdir, name=""):
    job = jobs[jid]
    cmd = ["yt-dlp", "--no-playlist", "--playlist-items", "1", "--newline",
           "-P", outdir, "-o", (name + ".%(ext)s") if name else "%(title).80s.%(ext)s"] + format_args(fmt) + ["--", url]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", start_new_session=True)
    except OSError:
        job.update(state="error", error=NOT_INSTALLED)
        return
    procs[jid] = proc
    audio_only = fmt.startswith("a:")
    streams, tail, errs, found = 0, [], [], []

    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        tail = (tail + [line])[-12:]
        if "ERROR:" in line:
            errs.append(line)
        if line.startswith("["):
            m = re.search(r'(?:Destination: |Merging formats into )"?(.+?)"?$', line)
            if m:
                found.append(m.group(1))

        if line.startswith("[download]"):
            if "Destination:" in line:
                streams += 1
                job.update(stage="Downloading audio" if audio_only or streams > 1
                           else "Downloading video",
                           pct=0, busy=False, speed="", eta="")
            elif "has already been downloaded" in line:
                m = re.match(r"\[download\] (.+) has already been downloaded", line)
                if m:
                    found.append(m.group(1))
                job.update(stage="Already downloaded", pct=100)
            else:
                pm = re.search(r"(\d+(?:\.\d+)?)%", line)
                if pm:
                    sm = re.search(r"\bat\s+(\S+/s)", line)
                    em = re.search(r"ETA\s+(\S+)", line)
                    eta = em.group(1) if em else ""
                    job.update(pct=float(pm.group(1)),
                               speed=sm.group(1) if sm else "",
                               eta="" if eta == "Unknown" else eta)
        elif line.startswith("[Merger]"):
            job.update(stage="Merging video and audio", pct=100, busy=True, speed="", eta="")
        elif line.startswith("[ExtractAudio]"):
            job.update(stage="Converting audio", pct=100, busy=True, speed="", eta="")
        elif line.startswith(("[VideoRemuxer]", "[VideoConvertor]")):
            job.update(stage="Finishing up", pct=100, busy=True, speed="", eta="")

    proc.wait()
    procs.pop(jid, None)

    path = None
    for c in reversed(found):
        cp = Path(c)
        if not cp.is_absolute():
            cp = Path(outdir) / c
        if cp.exists():
            path = cp
            break

    if job.get("_cancelled"):
        job.update(state="cancelled", stage="Cancelled", busy=False, speed="", eta="")
    elif proc.returncode == 0:
        job.update(state="done", pct=100, busy=False, stage="Done", speed="", eta="",
                   file=path.name if path else "", _path=str(path) if path else "")
        notify(path.name if path else "download complete")
    else:
        text = "\n".join(errs) if errs else "\n".join(tail)
        job.update(state="error", busy=False, error=friendly(text),
                   detail="\n".join(tail[-6:]))


def cancel_job(jid):
    job, proc = jobs.get(jid), procs.get(jid)
    if not job or not proc or job.get("state") != "running":
        return False
    job["_cancelled"] = True
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    def force():
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
    threading.Timer(3, force).start()
    return True


# ---------------------------------------------------------------- web page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt</title>
<style>
:root{--bg:#fff;--fg:#000;--mut:rgba(0,0,0,.55);--line:rgba(0,0,0,.2);--field:transparent;
 --panel:transparent;--pline:transparent;--btn:#000;--btnfg:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#000;--fg:#fff;--mut:rgba(255,255,255,.6);
 --line:rgba(255,255,255,.25);--btn:#fff;--btnfg:#000}}
body.amb{--fg:#fff;--mut:rgba(255,255,255,.74);--line:rgba(255,255,255,.34);
 --field:rgba(255,255,255,.07);--panel:rgba(0,0,0,.34);--pline:rgba(255,255,255,.18);
 --btn:#fff;--btnfg:#000}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--bg)}
body{margin:0;min-height:100vh;display:grid;place-items:center;color:var(--fg);
 font:15px/1.45 -apple-system,"SF Pro Text",BlinkMacSystemFont,"Helvetica Neue",sans-serif;
 letter-spacing:-.01em;-webkit-font-smoothing:antialiased;transition:color .8s}
#ambient{position:fixed;inset:0;z-index:-1;overflow:hidden;background:var(--bg)}
#ambient::after{content:"";position:absolute;inset:0;background:rgba(0,0,0,.3);opacity:0;transition:opacity 1.2s}
body.amb #ambient::after{opacity:1}
.layer{position:absolute;inset:-12%;background-size:cover;background-position:center;
 filter:blur(44px) saturate(1.6) brightness(.9);opacity:0;transition:opacity 1.4s ease}
.layer.on{opacity:1}
main{width:min(540px,92vw);padding:28px 0}
.panel{background:var(--panel);border:1px solid var(--pline);border-radius:22px;padding:24px;
 -webkit-backdrop-filter:blur(26px) saturate(1.2);backdrop-filter:blur(26px) saturate(1.2);
 transition:background-color .9s,border-color .9s}
label{display:block;margin:20px 0 7px;font-size:12.5px;color:var(--mut)}
.field{display:block;width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:10px;
 background:var(--field);color:var(--fg);font:inherit;letter-spacing:inherit;outline:none;
 transition:border-color .2s,background-color .9s}
.field:focus{border-color:var(--fg)}
.field::placeholder{color:var(--mut)}
#url{font-size:16px;padding:15px 16px}
.sel{position:relative}
.sel select{appearance:none;-webkit-appearance:none;padding-right:40px;cursor:pointer}
.sel::after{content:"";position:absolute;right:17px;top:50%;width:7px;height:7px;
 border-right:1.5px solid var(--fg);border-bottom:1.5px solid var(--fg);
 transform:translateY(-75%) rotate(45deg);opacity:.6;pointer-events:none}
select.field option{color:#000;background:#fff}
@media(prefers-color-scheme:dark){select.field option{color:#fff;background:#1c1c1e}}
.row{display:flex;gap:8px}
.btn{padding:13px 18px;border:1px solid var(--line);border-radius:10px;background:transparent;
 color:var(--fg);font:inherit;font-weight:500;letter-spacing:inherit;cursor:pointer;white-space:nowrap;
 transition:border-color .2s,opacity .2s,transform .1s,background-color .9s,color .9s}
.btn:hover{border-color:var(--fg)}
.btn:active{transform:scale(.99)}
.btn:focus-visible{outline:2px solid var(--fg);outline-offset:2px}
.btn:disabled,.field:disabled{opacity:.4;cursor:default}
.btn.pri{display:block;width:100%;margin-top:24px;padding:15px;background:var(--btn);
 color:var(--btnfg);border-color:var(--btn);font-weight:600}
.btn.pri:hover{opacity:.88}
.btn.wide{display:block;width:100%;margin-top:24px;padding:15px}

.pv{display:grid;grid-template-columns:140px 1fr;gap:16px;align-items:center;max-height:0;
 opacity:0;overflow:hidden;transition:max-height .6s ease,opacity .6s ease,margin .6s ease}
.pv.on{max-height:170px;opacity:1;margin-top:18px}
.pv.noimg{grid-template-columns:1fr}
.pv img{width:140px;aspect-ratio:16/9;object-fit:cover;border-radius:8px;display:block;
 background:var(--field)}
.pv.load img{visibility:hidden}
.pv.load .t{opacity:.5;animation:pulse 1.2s ease-in-out infinite}
.t{font:600 19px/1.25 ui-serif,"New York",Georgia,serif;letter-spacing:-.015em;
 display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.m{margin-top:5px;font-size:12.5px;color:var(--mut)}
.m span{display:block}
@keyframes pulse{50%{opacity:.2}}

.bar{height:3px;border-radius:3px;background:var(--line);margin-top:22px;overflow:hidden;
 opacity:0;transition:opacity .4s}
.bar.on{opacity:1}
.bar i{display:block;height:100%;width:0;background:var(--fg);border-radius:3px;
 transition:width .35s ease}
.bar.busy i{width:35%!important;animation:slide 1.1s ease-in-out infinite}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
.stat{display:flex;justify-content:space-between;gap:12px;margin-top:9px;min-height:1.4em;
 font-size:12.5px;color:var(--mut);font-variant-numeric:tabular-nums}
.msg{margin-top:12px;font-size:14px;word-break:break-word}
.msg .btn{margin-top:14px}
.msg details{margin-top:10px;font-size:12px;color:var(--mut)}
.msg summary{cursor:pointer}
.msg pre{white-space:pre-wrap;margin:8px 0 0;font:11.5px/1.5 ui-monospace,Menlo,monospace}
@media(max-width:420px){.pv{grid-template-columns:1fr}.pv img{width:100%}}
@media(prefers-reduced-motion:reduce){*{transition-duration:.01ms!important;animation-duration:.01ms!important}}
</style></head><body>
<div id="ambient"></div>
<main><div class="panel">
  <input id="url" class="field" placeholder="Paste a link" autocomplete="off"
         autocapitalize="off" spellcheck="false">
  <div id="pv" class="pv">
    <img id="pimg" alt="">
    <div><div id="pt" class="t"></div><div id="pm" class="m"></div></div>
  </div>
  <div id="nameWrap" hidden><label for="name">File name</label>
    <input id="name" class="field" spellcheck="false"></div>
  <label for="fmt">Quality</label>
  <div class="sel"><select id="fmt" class="field"></select></div>
  <label for="dir">Save to</label>
  <div class="row"><input id="dir" class="field" spellcheck="false">
    <button id="browse" class="btn">Browse</button></div>
  <button id="go" class="btn pri">Download</button>
  <button id="cancel" class="btn wide" hidden>Cancel</button>
  <div id="bar" class="bar"><i id="fill"></i></div>
  <div class="stat"><span id="st"></span><span id="sp"></span></div>
  <div id="msg" class="msg"></div>
</div></main>
<script>
const $=id=>document.getElementById(id), DEF=__DIR__;
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(b||{})}).then(r=>r.json());
let wanted=localStorage.fmt||"v:best", lastUrl="", infoSeq=0, ambSeq=0, timer=null;
let job=null, lastJob=null, poll=null, ticking=false, origTitle="";

/* ---------- quality list ---------- */
function pick(opts,want){
  if(opts.includes(want)) return want;
  if(want.startsWith("v:")&&want!=="v:best"){
    const h=+want.slice(2);
    const lower=opts.filter(v=>v.startsWith("v:")&&v!=="v:best").map(v=>+v.slice(2))
      .filter(x=>x<=h).sort((a,b)=>b-a);
    if(lower.length) return "v:"+lower[0];
  }
  return "v:best";
}
function buildFormats(heights){
  const sel=$("fmt"); sel.replaceChildren();
  const vids=[["v:best","Best available"]];
  if(heights===null){ vids.push(["v:1080","1080p"],["v:720","720p"]); }
  else heights.forEach(o=>vids.push(["v:"+o.h,o.h+"p"+(o.h264?"":" (may not open in QuickTime)")]));
  const groups=[["Video",vids],["Audio",[["a:mp3","MP3"],["a:m4a","M4A"]]]];
  const all=[];
  groups.forEach(([name,list])=>{
    const g=document.createElement("optgroup"); g.label=name;
    list.forEach(([v,t])=>{const o=document.createElement("option");o.value=v;o.textContent=t;g.append(o);all.push(v);});
    sel.append(g);
  });
  sel.value=pick(all,wanted);
}

/* ---------- ambient background ---------- */
function tiny(src){
  return new Promise(res=>{
    const im=new Image();
    im.onload=()=>{try{
      const w=48,h=Math.max(1,Math.round(w*im.naturalHeight/im.naturalWidth));
      const c=document.createElement("canvas");c.width=w;c.height=h;
      c.getContext("2d").drawImage(im,0,0,w,h);res(c.toDataURL("image/jpeg",.85));
    }catch(e){res(src);}};
    im.onerror=()=>res(null); im.src=src;
  });
}
function setAmbient(src){
  const box=$("ambient"), my=++ambSeq;
  const old=[...box.querySelectorAll(".layer")];
  if(!src){
    document.body.classList.remove("amb");
    old.forEach(o=>{o.classList.remove("on");setTimeout(()=>o.remove(),1600);});
    return;
  }
  tiny(src).then(u=>{
    if(!u||my!==ambSeq) return;
    const d=document.createElement("div"); d.className="layer";
    d.style.backgroundImage="url("+u+")"; box.append(d);
    requestAnimationFrame(()=>requestAnimationFrame(()=>d.classList.add("on")));
    document.body.classList.add("amb");
    old.forEach(o=>{o.classList.remove("on");setTimeout(()=>o.remove(),1600);});
  });
}

/* ---------- preview ---------- */
const fmtDur=s=>{
  if(!s) return ""; s=Math.round(s);
  const h=Math.floor(s/3600), m=Math.floor(s%3600/60), x=s%60;
  return (h?h+":"+String(m).padStart(2,"0"):m)+":"+String(x).padStart(2,"0");
};
function line(text){const s=document.createElement("span");s.textContent=text;return s;}
function clearPreview(){
  $("pv").classList.remove("on","load"); setAmbient(null); buildFormats(null); lastUrl="";
  $("nameWrap").hidden=true; $("name").value=""; origTitle="";
}
async function fetchInfo(){
  const url=$("url").value.trim();
  if(!/^https?:\/\//i.test(url)){ if(lastUrl) clearPreview(); return; }
  if(url===lastUrl) return;
  lastUrl=url; const my=++infoSeq;
  $("msg").replaceChildren();
  const pv=$("pv"); pv.classList.add("on","load"); pv.classList.remove("noimg");
  $("nameWrap").hidden=true; $("name").value=""; origTitle="";
  $("pt").textContent="Reading link"; $("pm").replaceChildren();
  let d;
  try{ d=await post("/info",{url}); }catch(e){ d={error:"Lost connection to the app."}; }
  if(my!==infoSeq) return;
  if(d.error){
    clearPreview(); lastUrl=""; showError(d.error); return;
  }
  pv.classList.remove("load");
  $("pt").textContent=d.title; origTitle=d.title; $("name").value=d.title; $("nameWrap").hidden=false;
  const pm=$("pm"); pm.replaceChildren();
  if(d.uploader) pm.append(line(d.uploader));
  if(d.duration) pm.append(line(fmtDur(d.duration)));
  if(d.playlist) pm.append(line("Playlist link: only the first video will download"));
  const im=$("pimg");
  if(d.thumb){ im.src=d.thumb; pv.classList.remove("noimg"); setAmbient(d.thumb); }
  else { im.removeAttribute("src"); pv.classList.add("noimg"); setAmbient(null); }
  buildFormats(d.heights);
}

/* ---------- messages ---------- */
function showError(text,detail){
  const m=$("msg"); m.replaceChildren();
  const t=document.createElement("div"); t.textContent=text; m.append(t);
  if(detail){
    const dt=document.createElement("details"), s=document.createElement("summary"),
          pre=document.createElement("pre");
    s.textContent="Details"; pre.textContent=detail; dt.append(s,pre); m.append(dt);
  }
}
function showDone(s){
  const m=$("msg"); m.replaceChildren();
  const t=document.createElement("div"); t.textContent="Saved"+(s.file?": "+s.file:""); m.append(t);
  const b=document.createElement("button"); b.className="btn"; b.textContent="Show in Finder";
  b.onclick=()=>post("/reveal",{id:lastJob}); m.append(b);
}

/* ---------- download ---------- */
function setRunning(on){
  $("go").hidden=on; $("cancel").hidden=!on; $("cancel").disabled=false;
  ["url","name","fmt","dir","browse"].forEach(id=>$(id).disabled=on);
}
function stopPolling(){ clearInterval(poll); poll=null; job=null; setRunning(false); }
async function go(){
  if(job) return;
  const url=$("url").value.trim();
  if(!/^https?:\/\//i.test(url)){ showError("Paste a video link first."); return; }
  localStorage.dir=$("dir").value; localStorage.fmt=wanted;
  $("msg").replaceChildren(); $("st").textContent="Starting"; $("sp").textContent="";
  $("fill").style.width="0"; $("bar").classList.add("on"); $("bar").classList.remove("busy");
  setRunning(true);
  let r;
  const nm=$("name").value.trim();
  const name=(!$("nameWrap").hidden&&nm&&nm!==origTitle)?nm:"";   // only send if edited
  try{ r=await post("/download",{url,format:$("fmt").value,dir:$("dir").value,name}); }
  catch(e){ r={error:"Lost connection to the app."}; }
  if(r.error){ stopPolling(); $("bar").classList.remove("on"); $("st").textContent=""; showError(r.error); return; }
  job=lastJob=r.id; poll=setInterval(tick,400); tick();
}
async function tick(){
  if(!job||ticking) return; ticking=true;
  let s;
  try{ s=await (await fetch("/status/"+job)).json(); }
  catch(e){ ticking=false; stopPolling(); showError("Lost connection to the app."); return; }
  ticking=false;
  if(!job) return;
  $("fill").style.width=(s.pct||0)+"%";
  $("bar").classList.toggle("busy",!!s.busy);
  $("st").textContent=s.stage||"";
  $("sp").textContent=[s.speed,(s.eta?"ETA "+s.eta:"")].filter(Boolean).join("   ");
  if(s.state==="running") return;
  stopPolling();
  if(s.state==="done"){ showDone(s); $("sp").textContent=""; $("url").focus(); $("url").select(); }
  else if(s.state==="cancelled"){ $("msg").textContent="Cancelled. Partial data is kept, so downloading again resumes."; }
  else showError(s.error||"Download failed.",s.detail);
  setTimeout(()=>{ if(!job) $("bar").classList.remove("on"); },1500);
}
$("go").onclick=go;
$("cancel").onclick=async()=>{
  if(!job) return; $("cancel").disabled=true; $("st").textContent="Cancelling";
  try{ await post("/cancel",{id:job}); }catch(e){}
};
$("browse").onclick=async()=>{
  const r=await post("/browse"); if(r.path){ $("dir").value=r.path; localStorage.dir=r.path; }
};
$("fmt").onchange=()=>{ wanted=$("fmt").value; localStorage.fmt=wanted; };
$("url").addEventListener("input",()=>{ clearTimeout(timer); timer=setTimeout(fetchInfo,450); });
$("url").addEventListener("paste",()=>setTimeout(fetchInfo,60));
["url","dir","name"].forEach(id=>$(id).addEventListener("keydown",e=>{
  if(e.key==="Enter"){ e.preventDefault(); go(); }}));

$("dir").value=localStorage.dir||DEF;
buildFormats(null);
$("url").focus();
</script></body></html>
""".replace("__DIR__", json.dumps(DEFAULT_DIR).replace("</", "<\\/"))


# ---------------------------------------------------------------- server

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def guard(self):
        """Only our own page on localhost may talk to this server."""
        if self.headers.get("Host") not in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT):
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT)

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return None
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 100000:
                return None
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        if not self.guard():
            return self.send(403, {})
        path = self.path.split("?")[0]
        if path == "/":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path.startswith("/status/"):
            job = jobs.get(path[8:])
            if not job:
                return self.send(200, {"state": "error", "error": "Unknown download."})
            return self.send(200, {k: v for k, v in job.items() if not k.startswith("_")})
        if path.startswith("/thumb/"):
            item = thumbs.get(path[7:])
            if not item:
                return self.send(404, {})
            return self.send(200, item[0], item[1], {"Cache-Control": "private, max-age=3600"})
        self.send(404, {})

    def do_POST(self):
        if not self.guard():
            return self.send(403, {"error": "forbidden"})
        data = self.read_json()
        if data is None:
            return self.send(400, {"error": "bad request"})

        if self.path == "/browse":
            try:
                r = subprocess.run(
                    ["osascript",
                     "-e", 'tell application "System Events" to activate',
                     "-e", 'POSIX path of (choose folder with prompt "Download location")'],
                    capture_output=True, encoding="utf-8", errors="replace")
                path = r.stdout.strip().rstrip("/")
            except OSError:
                path = ""
            return self.send(200, {"path": path})

        if self.path == "/info":
            url = str(data.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                return self.send(200, {"error": "That doesn't look like a link."})
            return self.send(200, get_info(url))

        if self.path == "/download":
            url = str(data.get("url", "")).strip()
            fmt = str(data.get("format", ""))
            if not url.startswith(("http://", "https://")):
                return self.send(200, {"error": "That doesn't look like a link."})
            if not FORMAT_RE.match(fmt):
                return self.send(200, {"error": "Pick a quality first."})
            outdir = Path(str(data.get("dir", "")).strip() or DEFAULT_DIR).expanduser()
            if not outdir.is_dir():
                return self.send(200, {"error": "That folder doesn't exist."})
            outdir = outdir.resolve()
            if not os.access(outdir, os.W_OK):
                return self.send(200, {"error": "Can't write to that folder. Choose another one."})
            name = clean_name(data.get("name", ""))
            jid = uuid.uuid4().hex
            jobs[jid] = {"state": "running", "pct": 0, "stage": "Starting", "speed": "",
                         "eta": "", "busy": False, "file": "", "error": "", "detail": "",
                         "_dir": str(outdir)}
            threading.Thread(target=run, args=(jid, url, fmt, str(outdir), name), daemon=True).start()
            return self.send(200, {"id": jid})

        if self.path == "/cancel":
            return self.send(200, {"ok": cancel_job(str(data.get("id", "")))})

        if self.path == "/reveal":
            job = jobs.get(str(data.get("id", "")))
            if job:
                reveal(job)
            return self.send(200, {"ok": bool(job)})

        self.send(404, {})


def _on_term(*_):
    raise KeyboardInterrupt


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        print("Already running at http://localhost:%d" % PORT)
        webbrowser.open("http://localhost:%d" % PORT)
        return
    signal.signal(signal.SIGTERM, _on_term)
    print("http://localhost:%d  (Ctrl+C to quit)" % PORT)
    webbrowser.open("http://localhost:%d" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for p in list(procs.values()):
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass


if __name__ == "__main__":
    main()
