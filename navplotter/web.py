"""The same small JSON API as the NavPlotter nodes, and the same web app (docs/PROTOCOL.md, section 8).

Unlike the nodes it also answers CORS requests, so that the Android app can call it from its own origin.
"""
import gzip
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("navplotter.web")
USERDATA_MAX = 32768
# Static files of the web app besides the page itself
WEB_FILES = {"/navplotter-meteo.js": "application/javascript; charset=utf-8"}
# /meteo asks these for the browser: the forecasts of the passage planning (web/navplotter-meteo.js)
METEO_HOSTS = ("api.open-meteo.com", "marine-api.open-meteo.com")


def make_handler(node, web_dir):
    index_path = os.path.join(web_dir, "index.html")
    with open(index_path, "rb") as f:
        index_gz = gzip.compress(f.read())
    files_gz = {}
    for name in WEB_FILES:
        with open(os.path.join(web_dir, name[1:]), "rb") as f:
            files_gz[name] = gzip.compress(f.read())
    userdata_path = os.path.join(node.s["data_dir"], "userdata.json")
    host = socket.gethostname() + ".local"

    class Handler(BaseHTTPRequestHandler):
        server_version = "NavPlotter"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, separators=(",", ":"))
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _form(self, body):
            # Parameters come in the body (URLSearchParams) or in the query string, as the ESP32 web server accepts
            q = urllib.parse.urlparse(self.path).query
            form = {k: v[-1] for k, v in urllib.parse.parse_qs(q, keep_blank_values=True).items()}
            if body:
                form.update({k: v[-1] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()})
            return form

        def _camera(self, path, body=None):
            """/camera/... goes to the camera service (python3 -m navplotter.camera), the video streamed as it comes."""
            url = "http://127.0.0.1:%d/%s" % (node.s["camera_port"], path[len("/camera/"):])
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=10)
            except (urllib.error.URLError, OSError):
                self._send(503, {"ok": False, "error": "camera service not running"})
                return
            with r:
                self.send_response(r.status)
                for k in ("Content-Type", "Content-Length", "Cache-Control"):
                    if r.headers.get(k):
                        self.send_header(k, r.headers[k])
                self._cors()
                self.end_headers()
                try:
                    while True:
                        chunk = r.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        def _meteo(self, query):
            """/meteo?url=... fetches an Open-Meteo forecast for the browser, which may have no internet of its own."""
            url = urllib.parse.parse_qs(query).get("url", [""])[0]
            u = urllib.parse.urlparse(url)
            if u.scheme != "https" or u.hostname not in METEO_HOSTS:
                self._send(400, {"error": True, "reason": "not an Open-Meteo address"})
                return
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "NavPlotter"}), timeout=30) as r:
                    self._send(r.status, r.read())
            except urllib.error.HTTPError as e:
                self._send(e.code, e.read() or {"error": True, "reason": "HTTP %d" % e.code})
            except (urllib.error.URLError, OSError) as e:
                self._send(502, {"error": True, "reason": "no internet on the Raspberry Pi (%s)" % getattr(e, "reason", e)})

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path.startswith("/camera/"):
                self._camera(path)
            elif path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(index_gz)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(index_gz)
            elif path in files_gz:
                self._send(200, files_gz[path], WEB_FILES[path], {"Content-Encoding": "gzip"})
            elif path == "/meteo":
                self._meteo(url.query)
            elif path == "/api/state":
                self._send(200, node.state_json())
            elif path == "/api/config":
                self._send(200, node.config_json(host))
            elif path == "/api/messages":
                self._send(200, node.messages_json())
            elif path == "/api/userdata":
                try:
                    with open(userdata_path, "rb") as f:
                        self._send(200, f.read())
                except FileNotFoundError:
                    self._send(200, "{}")
            elif path == "/api/track":
                start = urllib.parse.parse_qs(url.query).get("from", ["0"])[0]
                with node.lock:
                    self._send(200, node.track.points_json(int(start) if start.isdigit() else 0))
            elif path == "/api/track.gpx":
                with node.lock:
                    gpx, tid = node.track.gpx(node.name), node.track.id
                self._send(200, gpx, "application/gpx+xml",
                           {"Content-Disposition": 'attachment; filename="navplotter-track-%d.gpx"' % tid})
            elif path.startswith("/tiles/") and path.endswith(".jpg") and ".." not in path and len(path) < 48:
                f = os.path.join(node.s["tiles_dir"], path[len("/tiles/"):])
                try:
                    with open(f, "rb") as fh:
                        self._send(200, fh.read(), "image/jpeg", {"Cache-Control": "max-age=86400"})
                except OSError:
                    self._send(404, "Not found", "text/plain")
            else:
                self._send(404, "Not found", "text/plain")

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            body = self._body()
            if path.startswith("/camera/"):
                self._camera(path, body)
            elif path == "/api/config":
                node.apply_config(self._form(body))
                reply = node.config_json(host)
                reply["restart"] = False
                self._send(200, reply)
            elif path == "/api/userdata":
                if not body or len(body) > USERDATA_MAX or body[:1] != b"{":
                    self._send(400, {"ok": False})
                    return
                tmp = userdata_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(body)
                os.replace(tmp, userdata_path)
                self._send(200, {"ok": True})
            elif path == "/api/messages":
                form = self._form(body)
                text = form.get("text", "").strip()
                try:
                    to = int(form.get("to", "0") or "0", 16)
                except ValueError:
                    to = -1
                if not text or len(text) >= 64 or to < 0:
                    self._send(400, {"ok": False})
                    return
                node.send_message(to, form.get("name", ""), text)
                self._send(200, {"ok": True})
            elif path == "/api/messages/read":
                form = self._form(body)
                try:
                    peer = int(form.get("peer", "0") or "0", 16)
                except ValueError:
                    peer = 0
                node.messages_read(peer, "all" in form)
                self._send(200, {"ok": True})
            elif path == "/api/quiet":
                form = self._form(body)
                try:
                    with node.lock:
                        node.traffic.set_quiet(int(form.get("id", "0"), 16), form.get("on") == "1")
                except ValueError:
                    pass
                self._send(200, {"ok": True})
            elif path == "/api/mute":
                self._send(200, {"ok": True})       # no buzzer on the Raspberry Pi
            elif path == "/api/track/new":
                with node.lock:
                    node.track.new()
                self._send(200, {"ok": True})
            else:
                self._send(404, "Not found", "text/plain")

    return Handler


def start(node, web_dir, port):
    server = ThreadingHTTPServer(("", port), make_handler(node, web_dir))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web app on port %d", port)
    return server
