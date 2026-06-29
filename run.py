#!/usr/bin/env python3
# Pattermesh's one file to run, test, and collect logs on the switchboard devserver.
#
# - `python run.py`            -> serve web/ lab, open browser, capture client logs
# - `python run.py serve`      -> same as above (explicit)
# - `python run.py test ...`   -> run pytest, capture output to the session folder
#
# Every invocation creates a per-session subfolder under ./.session/<timestamp>/
# holding: browser.jsonl (console + errors + network from the live page),
# server.log (access log), meta.json (summary), and pytest.log (test mode).
#
# Pure stdlib only. Python 3.11+.  Ctrl-C shuts down gracefully and finalizes meta.json.

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent
WEB_DIR = REPO / "web"
SESSION_ROOT = REPO / ".session"
LOG_ENDPOINT = "/__session/log"

# ---------------------------------------------------------------------------
# Browser-side capture shim: injected into every served HTML page. It wraps
# console.*, window error/rejection events, and fetch/XHR, then beacons each
# event to LOG_ENDPOINT so the server can persist a full client trace.
# ---------------------------------------------------------------------------
CAPTURE_SHIM = """
<script>
(function(){
  if (window.__sbCapture) return; window.__sbCapture = true;
  var ENDPOINT = "%ENDPOINT%", queue = [], flushing = false;
  function send(ev){ ev.t = new Date().toISOString(); ev.page = location.pathname; queue.push(ev); schedule(); }
  function schedule(){ if (flushing) return; flushing = true; setTimeout(flush, 200); }
  function flush(){
    flushing = false; if (!queue.length) return;
    var batch = queue.splice(0, queue.length);
    try { fetch(ENDPOINT, {method:"POST", headers:{"Content-Type":"application/json"},
                           body: JSON.stringify(batch), keepalive:true}).catch(function(){}); } catch(e){}
  }
  ["log","info","warn","error","debug"].forEach(function(level){
    var orig = console[level] ? console[level].bind(console) : function(){};
    console[level] = function(){
      try {
        var args = Array.prototype.slice.call(arguments).map(function(a){
          try { return (typeof a === "object") ? JSON.stringify(a) : String(a); } catch(e){ return String(a); }
        });
        send({kind:"console", level:level, msg:args.join(" ")});
      } catch(e){}
      return orig.apply(null, arguments);
    };
  });
  window.addEventListener("error", function(e){
    send({kind:"error", level:"error", msg:(e&&e.message)||"error",
          src:(e&&e.filename)||"", line:(e&&e.lineno)||0, col:(e&&e.colno)||0});
  });
  window.addEventListener("unhandledrejection", function(e){
    var r = e && e.reason, msg;
    try { msg = (r && r.stack) || String(r); } catch(_){ msg = "unhandledrejection"; }
    send({kind:"unhandledrejection", level:"error", msg:msg});
  });
  if (window.fetch){
    var of = window.fetch.bind(window);
    window.fetch = function(input, init){
      var u = (typeof input === "string") ? input : (input && input.url) || "";
      if (u.indexOf(ENDPOINT) !== -1) return of(input, init);
      var m = (init && init.method) || (input && input.method) || "GET", t0 = performance.now();
      send({kind:"network", level:"info", phase:"request", method:m, target:u});
      return of(input, init).then(function(res){
        send({kind:"network", level:(res.status>=400?"error":"info"), phase:"response",
              method:m, target:u, status:res.status, ms:Math.round(performance.now()-t0)});
        return res;
      }).catch(function(err){
        send({kind:"network", level:"error", phase:"error", method:m, target:u, msg:String(err)}); throw err;
      });
    };
  }
  (function(){
    var X = window.XMLHttpRequest; if (!X) return;
    var open = X.prototype.open, sendm = X.prototype.send;
    X.prototype.open = function(m,u){ this.__sb={method:m,url:u,t0:0}; return open.apply(this, arguments); };
    X.prototype.send = function(){
      var self=this, info=this.__sb||{};
      if (String(info.url||"").indexOf(ENDPOINT)===-1){
        info.t0 = performance.now();
        send({kind:"network", level:"info", phase:"request", method:info.method, target:info.url, transport:"xhr"});
        this.addEventListener("loadend", function(){
          send({kind:"network", level:(self.status>=400?"error":"info"), phase:"response",
                method:info.method, target:info.url, status:self.status,
                ms:Math.round(performance.now()-(info.t0||0)), transport:"xhr"});
        });
      }
      return sendm.apply(this, arguments);
    };
  })();
  function beacon(){ try { if (queue.length) navigator.sendBeacon(ENDPOINT, JSON.stringify(queue.splice(0,queue.length))); } catch(e){} }
  window.addEventListener("visibilitychange", function(){ if (document.visibilityState==="hidden") beacon(); });
  window.addEventListener("pagehide", beacon);
  send({kind:"session", level:"info", msg:"capture-attached", ua:navigator.userAgent});
})();
</script>
""".replace("%ENDPOINT%", LOG_ENDPOINT)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Session:
    """Holds the per-session folder, files, counters, and a write lock."""

    def __init__(self, name: str | None) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.id = f"{stamp}-{name}" if name else stamp
        self.dir = SESSION_ROOT / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.browser_log = self.dir / "browser.jsonl"
        self.server_log = self.dir / "server.log"
        self.meta_path = self.dir / "meta.json"
        self.started = now_iso()
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.requests = 0

    def record_events(self, events: list) -> None:
        with self.lock:
            with self.browser_log.open("a", encoding="utf-8") as fh:
                for ev in events:
                    if not isinstance(ev, dict):
                        ev = {"kind": "raw", "msg": str(ev)}
                    ev["received_at"] = now_iso()
                    fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    kind = str(ev.get("kind", "unknown"))
                    self.counts[kind] = self.counts.get(kind, 0) + 1

    def record_request(self, line: str) -> None:
        with self.lock:
            self.requests += 1
            with self.server_log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def write_meta(self, **extra) -> None:
        with self.lock:
            meta = {
                "session_id": self.id,
                "started": self.started,
                "ended": extra.pop("ended", None),
                "requests_served": self.requests,
                "event_counts": dict(self.counts),
                "total_events": sum(self.counts.values()),
                **extra,
            }
            self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def make_handler(session: Session):
    class Handler(SimpleHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if self.path.split("?")[0] != LOG_ENDPOINT:
                self.send_error(404, "Not Found")
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"[]"
            try:
                payload = json.loads(raw.decode("utf-8") or "[]")
            except json.JSONDecodeError:
                payload = [{"kind": "raw", "msg": raw.decode("utf-8", "replace")}]
            session.record_events(payload if isinstance(payload, list) else [payload])
            self.send_response(204)
            self.end_headers()

        def do_GET(self):  # noqa: N802
            path = Path(self.translate_path(self.path))
            if path.is_dir():
                path = path / "index.html"
            if path.suffix == ".html" and path.is_file():
                self._serve_html(path)
            else:
                super().do_GET()

        def _serve_html(self, path: Path) -> None:
            html = path.read_text(encoding="utf-8", errors="replace")
            if "</body>" in html:
                html = html.replace("</body>", CAPTURE_SHIM + "</body>", 1)
            elif "</html>" in html:
                html = html.replace("</html>", CAPTURE_SHIM + "</html>", 1)
            else:
                html += CAPTURE_SHIM
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # quiet stdout; persist to server.log
            session.record_request(f"{now_iso()} {self.address_string()} {fmt % args}")

    return Handler


def find_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"no free port in {preferred}..{preferred + 19}")


def cmd_serve(args) -> int:
    if not WEB_DIR.is_dir():
        print(f"error: {WEB_DIR} not found", file=sys.stderr)
        return 1
    session = Session(args.session_name)
    port = find_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"
    handler = partial(make_handler(session), directory=str(WEB_DIR))
    httpd = ThreadingHTTPServer((args.host, port), handler)
    session.write_meta(mode="serve", url=url, root=str(WEB_DIR))

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    print(f"  switchboard devserver  →  {url}")
    print(f"  serving                →  {WEB_DIR}")
    print(f"  session logs           →  {session.dir}")
    print("  (Ctrl-C to stop & finalize)\n")

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)

    stop.wait()
    print("\n  shutting down…")
    httpd.shutdown()
    session.write_meta(mode="serve", url=url, root=str(WEB_DIR), ended=now_iso())
    print(f"  captured {sum(session.counts.values())} client events across "
          f"{session.requests} requests")
    print(f"  → {session.browser_log}")
    print(f"  → {session.meta_path}")
    return 0


def cmd_test(args) -> int:
    session = Session(args.session_name)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), str(REPO / "src"), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    cmd = [sys.executable, "-m", "pytest", *args.pytest_args]
    print(f"  running: {' '.join(cmd)}")
    print(f"  logging → {session.dir / 'pytest.log'}\n")
    log_path = session.dir / "pytest.log"
    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd, cwd=str(REPO), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout:  # tee to console + file
            sys.stdout.write(line)
            logf.write(line)
        rc = proc.wait()
    session.write_meta(mode="test", command=cmd, returncode=rc, ended=now_iso())
    print(f"\n  pytest exited {rc} — log at {log_path}")
    return rc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Pattermesh's switchboard devserver runner.")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="serve the web/ lab and capture client logs")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--no-browser", action="store_true", help="don't auto-open a browser")
    p_serve.add_argument("--session-name", default=None, help="suffix for the session folder")
    p_serve.set_defaults(func=cmd_serve)

    p_test = sub.add_parser("test", help="run pytest and capture output to the session folder")
    p_test.add_argument("--session-name", default=None)
    p_test.add_argument("pytest_args", nargs=argparse.REMAINDER, help="args passed to pytest")
    p_test.set_defaults(func=cmd_test)

    args = parser.parse_args(argv)
    if not args.command:  # default = serve
        args = parser.parse_args(["serve", *argv])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
