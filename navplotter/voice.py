"""python3 -m navplotter.voice: the voice assistant, all on the Raspberry, without internet.

Speech to text with whisper.cpp (whisper-server, the model kept loaded), the answer from the node's own data
(navplotter/assistant.py) or from a small local language model (llama.cpp, llama-server, if installed), and the
answer spoken with Piper. It runs as a service of the desktop user (systemctl --user), because the audio of a
Bluetooth headset lives in that user's PipeWire; tools/voice-install.sh sets it up.

It listens only on 127.0.0.1; the node's web server passes /voice/... to it:
  GET  /status        JSON: ready parts, state (idle, listening, thinking, speaking), last question and answer
  POST /listen        on=1 starts listening (a short beep), on=0 stops and answers; it stops by itself after 15 s
  POST /ask           text=...: a question typed instead of said
  POST /stop          stops listening or speaking
"""
import argparse
import json
import logging
import math
import os
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import assistant

log = logging.getLogger("navplotter.voice")

HOME = os.path.expanduser("~/.local/share/navplotter-voice")
LISTEN_MAX_S = 15
RATE = 16000
# Words Whisper should expect: it then writes them right
PROMPT = ("NavPlotter, waypoint, rotta, nodi, miglia, allarme collisione, messaggio a tutti, barche vicine, "
          "destinazione, velocità, posizione.")


def beep_wav(path, freq=880, ms=120):
    n = int(RATE * ms / 1000)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / RATE)
                                                      * min(1, i / 200, (n - i) / 200))) for i in range(n)))


def first_tool(*names):
    for n in names:
        if shutil.which(n):
            return n
    return None


class Parts:
    """Where the programs and models are (installed by tools/voice-install.sh), and the commands to use them."""

    def __init__(self, home):
        self.home = home
        find = lambda *p: next((x for x in p if os.path.exists(x)), None)
        self.whisper_server = find(os.path.join(home, "whisper.cpp/build/bin/whisper-server"))
        models = sorted(f for f in os.listdir(os.path.join(home, "models")) if f.startswith("ggml-")) \
            if os.path.isdir(os.path.join(home, "models")) else []
        self.whisper_model = os.path.join(home, "models", models[-1]) if models else None
        for pref in ("ggml-small", "ggml-base", "ggml-tiny"):        # the best one that is there
            hit = [m for m in models if m.startswith(pref)]
            if hit:
                self.whisper_model = os.path.join(home, "models", hit[0])
                break
        self.piper = find(os.path.join(home, "piper/piper"))
        voices = sorted(f for f in os.listdir(os.path.join(home, "voices")) if f.endswith(".onnx")) \
            if os.path.isdir(os.path.join(home, "voices")) else []
        self.voice = os.path.join(home, "voices", voices[0]) if voices else None
        self.llama_server = find(os.path.join(home, "llama.cpp/build/bin/llama-server"))
        ggufs = sorted(f for f in os.listdir(os.path.join(home, "llm")) if f.endswith(".gguf")) \
            if os.path.isdir(os.path.join(home, "llm")) else []
        self.llm_model = os.path.join(home, "llm", ggufs[0]) if ggufs else None
        self.record = first_tool("pw-record", "parecord")
        self.play = first_tool("pw-play", "paplay", "aplay")


class Server:
    """A helper program (whisper-server, llama-server) started and restarted by us."""

    def __init__(self, name, cmd, url):
        self.name, self.cmd, self.url = name, cmd, url
        self.proc = None
        self.ok = False
        threading.Thread(target=self._run, name=name, daemon=True).start()

    def _run(self):
        while True:
            log.info("starting %s", self.name)
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while self.proc.poll() is None:
                if not self.ok:
                    try:
                        urllib.request.urlopen(self.url, timeout=2).close()
                        self.ok = True
                        log.info("%s ready", self.name)
                    except Exception:
                        pass
                time.sleep(1)
            self.ok = False
            log.warning("%s stopped (%s), starting it again", self.name, self.proc.returncode)
            time.sleep(5)


def multipart(fields, files):
    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())
    for k, (name, data) in files.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                    "Content-Type: audio/wav\r\n\r\n" % (boundary, k, name)).encode() + data + b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode())
    return b"".join(out), "multipart/form-data; boundary=" + boundary


class Voice:
    def __init__(self, args, parts=None):
        self.args = args
        self.parts = parts or Parts(args.home)
        self.lock = threading.Lock()
        self.state = "idle"
        self.heard = ""
        self.reply = ""
        self.error = None
        self.history = []           # [(question, answer)], the last ones
        self.rec = None             # the recording process
        self.rec_file = None
        self.rec_started = 0.0
        self.player = None
        self.tmp = tempfile.mkdtemp(prefix="navplotter-voice-")
        self.beep = os.path.join(self.tmp, "beep.wav")
        beep_wav(self.beep)
        p = self.parts
        self.stt = self.llm = None
        if p.whisper_server and p.whisper_model:
            self.stt = Server("whisper-server", [p.whisper_server, "-m", p.whisper_model, "-l", "it", "-t", "4",
                                                 "--host", "127.0.0.1", "--port", str(args.stt_port),
                                                 "--prompt", PROMPT],
                              "http://127.0.0.1:%d/" % args.stt_port)
        if p.llama_server and p.llm_model:
            self.llm = Server("llama-server", [p.llama_server, "-m", p.llm_model, "-c", "2048", "-t", "4",
                                               "--host", "127.0.0.1", "--port", str(args.llm_port)],
                              "http://127.0.0.1:%d/health" % args.llm_port)
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---- listening ----

    def start_listening(self):
        with self.lock:
            if self.state == "listening":
                return
            self.stop_speaking()
            if not self.parts.record:
                self.error = "no recorder (pw-record or parecord): sudo apt install pipewire-bin"
                return
            self.rec_file = os.path.join(self.tmp, "question.wav")
            cmd = [self.parts.record, "--rate", str(RATE), "--channels", "1"]
            if self.parts.record == "pw-record":
                cmd += ["--format", "s16"]
            else:
                cmd += ["--format=s16le", "--file-format=wav"]
            self.rec = subprocess.Popen(cmd + [self.rec_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.rec_started = time.monotonic()
            self.state, self.heard, self.reply, self.error = "listening", "", "", None
            # After the recording has started: a Bluetooth headset switches to its microphone first, and the beep
            # then says when to speak
            self._play(self.beep)

    def stop_listening(self):
        with self.lock:
            if self.state != "listening" or not self.rec:
                return
            self.rec.send_signal(signal.SIGINT)      # the recorder then closes the WAV file properly
            try:
                self.rec.wait(3)
            except subprocess.TimeoutExpired:
                self.rec.kill()
            self.rec = None
            self.state = "thinking"
        threading.Thread(target=self._from_audio, daemon=True).start()

    def _watchdog(self):
        while True:
            time.sleep(0.5)
            if self.state == "listening" and time.monotonic() - self.rec_started > LISTEN_MAX_S:
                self.stop_listening()

    def _from_audio(self):
        if not os.path.exists(self.rec_file) or os.path.getsize(self.rec_file) < 1000:
            self._answer("")                    # stopped before anything was recorded
            return
        try:
            text = self.transcribe(self.rec_file)
        except Exception as e:
            log.warning("speech to text: %s", e)
            self._done("", "Non riesco a capire l'audio: il riconoscimento vocale non è pronto.", str(e))
            return
        self._answer(text)

    def ask(self, text):
        with self.lock:
            if self.state in ("listening", "thinking"):
                return
            self.stop_speaking()
            self.state, self.heard, self.reply, self.error = "thinking", text, "", None
        threading.Thread(target=self._answer, args=(text,), daemon=True).start()

    # ---- the three steps ----

    def transcribe(self, wav):
        if not (self.stt and self.stt.ok):
            raise RuntimeError("whisper-server not ready")
        with open(wav, "rb") as f:
            body, ctype = multipart({"response_format": "json", "temperature": "0.0"}, {"file": ("q.wav", f.read())})
        req = urllib.request.Request("http://127.0.0.1:%d/inference" % self.args.stt_port, data=body,
                                     headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.loads(r.read()).get("text", "")
        text = " ".join(text.split())
        # Whisper writes these for silence
        if text.strip(" .").lower() in ("", "[blank_audio]", "sottotitoli a cura di qtss", "grazie per la visione"):
            return ""
        return text

    def node(self, path, form=None):
        url = self.args.node + path
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5) as r:
            return json.loads(r.read() or b"null")

    def _answer(self, text):
        self.heard = text
        data = {}
        for k, path in (("state", "/api/state"), ("messages", "/api/messages"), ("userdata", "/api/userdata")):
            try:
                data[k] = self.node(path)
            except Exception as e:
                log.warning("node %s: %s", path, e)
        reply, action = assistant.answer(text, data)
        if reply is None:
            reply = self.ask_llm(text, data) or "Non ho capito. " + assistant.HELP
        if action and action[0] == "send":
            try:
                self.node("/api/messages", {"to": action[1], "name": action[2], "text": action[3]})
            except Exception as e:
                reply = "Il messaggio non è partito: il nodo non risponde."
                log.warning("send: %s", e)
        self._done(text, reply)

    def ask_llm(self, text, data):
        if not (self.llm and self.llm.ok):
            return None
        body = json.dumps({"messages": assistant.llm_messages(text, data), "max_tokens": 120,
                           "temperature": 0.3}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % self.args.llm_port, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.loads(r.read())["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning("language model: %s", e)
            return None
        return " ".join(out.replace("*", "").split())

    def _done(self, heard, reply, error=None):
        with self.lock:
            self.heard, self.reply, self.error = heard, reply, error
            self.history = (self.history + [(heard, reply)])[-10:]
            self.state = "speaking"
        log.info("Q: %s | A: %s", heard, reply)
        try:
            self.speak(reply)
        except Exception as e:
            log.warning("speak: %s", e)
            self.error = "Piper: %s" % e
        with self.lock:
            if self.state == "speaking":
                self.state = "idle"

    def speak(self, text):
        p = self.parts
        if not (p.piper and p.voice):
            raise RuntimeError("Piper or its Italian voice is not installed")
        out = os.path.join(self.tmp, "answer.wav")
        subprocess.run([p.piper, "--model", p.voice, "--output_file", out], input=text.encode(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60)
        self._play(out, wait=True)

    def _play(self, wav, wait=False):
        if not self.parts.play:
            return
        self.player = subprocess.Popen([self.parts.play, wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:
            try:
                self.player.wait(120)
            except subprocess.TimeoutExpired:
                self.player.kill()

    def stop_speaking(self):
        if self.player and self.player.poll() is None:
            self.player.terminate()

    def stop(self):
        if self.state == "listening":
            with self.lock:
                if self.rec:
                    self.rec.kill()
                    self.rec = None
                self.state = "idle"
        self.stop_speaking()

    def status(self):
        p = self.parts
        return {"state": self.state, "heard": self.heard, "reply": self.reply, "error": self.error,
                "stt": bool(self.stt and self.stt.ok), "tts": bool(p.piper and p.voice),
                "llm": bool(self.llm and self.llm.ok), "mic": bool(p.record),
                "sttModel": os.path.basename(p.whisper_model) if p.whisper_model else None,
                "llmModel": os.path.basename(p.llm_model) if p.llm_model else None,
                "history": [{"q": q, "a": a} for q, a in self.history]}


def make_handler(v):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NavPlotterVoice"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _json(self, obj):
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urllib.parse.urlparse(self.path).path == "/status":
                self._json(v.status())
            else:
                self.send_error(404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: x[-1] for k, x in urllib.parse.parse_qs(self.rfile.read(n).decode() if n else "").items()}
            path = urllib.parse.urlparse(self.path).path
            if path == "/listen":
                v.start_listening() if form.get("on") == "1" else v.stop_listening()
            elif path == "/ask":
                if form.get("text", "").strip():
                    v.ask(form["text"].strip()[:300])
            elif path == "/stop":
                v.stop()
            else:
                self.send_error(404)
                return
            self._json(v.status())

    return Handler


def main():
    ap = argparse.ArgumentParser(prog="navplotter.voice", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--node", default="http://127.0.0.1", help="the node's web server")
    ap.add_argument("--home", default=HOME, help="programs and models (tools/voice-install.sh)")
    ap.add_argument("--stt-port", type=int, default=8084)
    ap.add_argument("--llm-port", type=int, default=8085)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    v = Voice(args)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(v))
    server.daemon_threads = True
    log.info("voice service on 127.0.0.1:%d (speech to text %s, voice %s, language model %s)", args.port,
             v.parts.whisper_model or "missing", v.parts.voice or "missing", v.parts.llm_model or "none")
    server.serve_forever()


if __name__ == "__main__":
    main()
