#!/usr/bin/env python3
import json, re, subprocess, threading, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
DEFAULT_DIR = str(Path.home() / "Downloads")
jobs = {}

MP4 = ["--merge-output-format", "mp4"]
FORMATS = {
    "video-best": ["-S", "vcodec:h264,res,acodec:m4a"] + MP4,
    "video-1080": ["-S", "res:1080,vcodec:h264,acodec:m4a"] + MP4,
    "video-720":  ["-S", "res:720,vcodec:h264,acodec:m4a"] + MP4,
    "audio-mp3":  ["-x", "--audio-format", "mp3", "--audio-quality", "0"],
    "audio-m4a":  ["-x", "--audio-format", "m4a"],
}

def run(job, url, fmt, outdir):
    cmd = ["yt-dlp", "--no-playlist", "--newline",
           "-o", f"{outdir}/%(title).80s.%(ext)s", *FORMATS[fmt], "--", url]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    except FileNotFoundError:
        job.update(state="error", log="yt-dlp not found in PATH")
        return
    for line in p.stdout:
        line = line.strip()
        m = re.search(r"(\d+(?:\.\d+)?)%", line)
        if line.startswith("[download]") and m:
            job["pct"] = float(m.group(1))
        if line:
            job["log"] = line[:200]
    p.wait()
    if p.returncode == 0:
        job.update(state="done", pct=100, log="done")
    else:
        job["state"] = "error"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#fff;
 color:#000;font:15px/1.4 ui-monospace,Menlo,monospace}
main{width:min(520px,90vw)}
label{display:block;margin:18px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.08em}
input,select,button{width:100%;padding:12px;border:1px solid #000;border-radius:0;
 font:inherit;background:#fff;color:#000;outline:none}
.row{display:flex;gap:8px}
.row button{width:auto;white-space:nowrap}
#go{margin-top:24px;background:#000;color:#fff;cursor:pointer}
#go:disabled{opacity:.4;cursor:default}
button{cursor:pointer}
#bar{height:2px;background:#000;width:0;margin-top:20px;transition:width .3s}
#log{margin-top:8px;min-height:1.4em;font-size:12px;word-break:break-all;opacity:.7}
@media(prefers-color-scheme:dark){
 body{background:#000;color:#fff}
 input,select,button{background:#000;color:#fff;border-color:#fff}
 #go{background:#fff;color:#000}#bar{background:#fff}}
</style></head><body><main>
<label>Link</label>
<input id="url" placeholder="paste url" autocomplete="off" autocapitalize="off" spellcheck="false">
<label>Format</label>
<select id="fmt">
 <option value="video-best">Video - best (mp4)</option>
 <option value="video-1080">Video - up to 1080p (mp4)</option>
 <option value="video-720">Video - up to 720p (mp4)</option>
 <option value="audio-mp3">Audio - mp3</option>
 <option value="audio-m4a">Audio - m4a</option>
</select>
<label>Save to</label>
<div class="row"><input id="dir"><button id="browse">Browse</button></div>
<button id="go">Download</button>
<div id="bar"></div><div id="log"></div>
</main>
<script>
const $=id=>document.getElementById(id), DEF=__DIR__;
$("dir").value=localStorage.dir||DEF;
$("fmt").value=localStorage.fmt||"video-best";
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})}).then(r=>r.json());
$("browse").onclick=async()=>{const r=await post("/browse");if(r.path)$("dir").value=r.path;};
$("go").onclick=async()=>{
  const url=$("url").value.trim(); if(!url)return;
  localStorage.dir=$("dir").value; localStorage.fmt=$("fmt").value;
  $("go").disabled=true; $("bar").style.width="0"; $("log").textContent="starting...";
  const r=await post("/download",{url,format:$("fmt").value,dir:$("dir").value});
  if(r.error){$("log").textContent="error: "+r.error;$("go").disabled=false;return;}
  const t=setInterval(async()=>{
    const s=await (await fetch("/status/"+r.id)).json();
    $("bar").style.width=(s.pct||0)+"%"; $("log").textContent=s.log||"";
    if(s.state!=="running"){clearInterval(t);$("go").disabled=false;
      if(s.state==="done")$("url").value="";}
  },500);
};
</script></body></html>
""".replace("__DIR__", json.dumps(DEFAULT_DIR))

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def host_ok(self):
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.host_ok(): return self.send(403, {})
        if self.path == "/":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path.startswith("/status/"):
            return self.send(200, jobs.get(self.path[8:], {"state": "error", "log": "unknown job"}))
        self.send(404, {})

    def do_POST(self):
        if not self.host_ok(): return self.send(403, {})
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}")

        if self.path == "/browse":
            r = subprocess.run(
                ["osascript",
                 "-e", 'tell application "System Events" to activate',
                 "-e", 'POSIX path of (choose folder with prompt "Download location")'],
                capture_output=True, text=True)
            return self.send(200, {"path": r.stdout.strip().rstrip("/") or ""})

        if self.path == "/download":
            url = data.get("url", "").strip()
            fmt = data.get("format")
            outdir = Path(data.get("dir", "")).expanduser()
            if not url.startswith(("http://", "https://")):
                return self.send(200, {"error": "invalid url"})
            if fmt not in FORMATS:
                return self.send(200, {"error": "invalid format"})
            if not outdir.is_dir():
                return self.send(200, {"error": "folder does not exist"})
            jid = uuid.uuid4().hex
            jobs[jid] = {"state": "running", "pct": 0, "log": "starting..."}
            threading.Thread(target=run, args=(jobs[jid], url, fmt, str(outdir)),
                             daemon=True).start()
            return self.send(200, {"id": jid})
        self.send(404, {})

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"http://localhost:{PORT}  (Ctrl+C to quit)")
    webbrowser.open(f"http://localhost:{PORT}")
    srv.serve_forever()
