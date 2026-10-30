"""python3 -m navplotter.voice: the voice assistant, all on the Raspberry, without internet.

Speech to text with whisper.cpp (whisper-server, the model kept loaded), the answer from the node's own data
(navplotter/assistant.py) or from a small local language model (llama.cpp, llama-server, if installed), and the
answer spoken with Piper. It runs as a service of the desktop user (systemctl --user), because the audio of a
Bluetooth headset lives in that user's PipeWire; tools/voice-install.sh sets it up.

With "always" on it listens all the time: it cuts what it hears into phrases (by loudness), and a phrase that
starts with the wake word ("computer, dove siamo?") is a question; the wake word alone gives a beep, and then the
next phrase is the question. Just after an answer the next phrase needs no wake word, for a conversation.
With "course" on it says when the course over ground stays off the bearing to the destination waypoint.
The settings are in config.json in --home.

It listens only on 127.0.0.1; the node's web server passes /voice/... to it:
  GET  /status        JSON: ready parts, state (idle, listening, thinking, speaking), last question and answer
  POST /listen        on=1 starts listening (a short beep), on=0 stops and answers; it stops by itself after 15 s
  POST /ask           text=...: a question typed instead of said
  POST /stop          stops listening or speaking
  POST /config        always=1/0, wake=..., course=1/0, courseDeg=...
"""
import array
import collections
import queue
import re
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
CONFIG = {"always": False, "wake": "computer", "course": True, "courseDeg": 20}
FOLLOW_S = 8                # after an answer (or the wake word alone), the next phrase needs no wake word
FRAME = 480                 # 30 ms of audio, the step of the phrase cutter
MIN_RMS = 250               # quieter than this is never a voice
COURSE_EVERY_S = 5
COURSE_SAMPLES = 6          # off course on average over 30 s before saying it
COURSE_REPEAT_S = 120
# Words Whisper should expect: it then writes them right
PROMPT = ("Computer, NavPlotter, waypoint, rotta, nodi, miglia, allarme collisione, messaggio a tutti, barche vicine, "
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
        # A smaller model, if there is one, for the phrases heard all the time: is it the wake word?
        self.wake_model = next((os.path.join(home, "models", m) for pref in ("ggml-base", "ggml-tiny") for m in models
                                if m.startswith(pref) and os.path.join(home, "models", m) != self.whisper_model), None)
        self.piper = find(os.path.join(home, "piper/piper"))
        voices = sorted(f for f in os.listdir(os.path.join(home, "voices")) if f.endswith(".onnx")) \
            if os.path.isdir(os.path.join(home, "voices")) else []
        self.voice = os.path.join(home, "voices", voices[0]) if voices else None
        self.llama_server = find(os.path.join(home, "llama.cpp/build/bin/llama-server"))
        ggufs = sorted(f for f in os.listdir(os.path.join(home, "llm")) if f.endswith(".gguf")) \
            if os.path.isdir(os.path.join(home, "llm")) else []
        self.llm_model = os.path.join(home, "llm", ggufs[0]) if ggufs else None
        self.record = first_tool("pw-record", "parecord")
        self.stream = first_tool("parecord", "pw-record")      # raw audio on stdout, for listening all the time
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


def rms(pcm):
    a = array.array("h", pcm)
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def write_wav(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def after_wake(text, wake):
    """The words after the wake word, or None if the phrase does not start with it (Whisper may put a word or two
    of noise in front)."""
    words = assistant.normalize(text).split()
    names = [assistant.normalize(w) for w in wake.split(",") if w.strip()]
    for i in range(min(3, len(words))):
        for n in names:
            k = len(n.split())
            if " ".join(words[i:i + k]) == n:
                return " ".join(words[i + k:])
    return None


def strip_wake(text, wake):
    """The question as Whisper wrote it, without the wake word in front."""
    for n in [w.strip() for w in wake.split(",") if w.strip()]:
        m = re.search(r"\b" + r"\W+".join(map(re.escape, n.split())) + r"\b\W*", text, re.I)
        if m and m.start() < 25:
            return text[m.end():].strip()
    return text


def sentences(chunks):
    """Whole sentences out of the pieces of text the language model streams, to speak the first one at once."""
    buf = ""
    for c in chunks:
        buf += c
        while True:
            m = re.search(r"[.!?;](\s+)", buf)
            if not m:
                break
            yield buf[:m.start() + 1].strip()
            buf = buf[m.end():]
    if buf.strip():
        yield buf.strip()


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
        self.cfg = dict(CONFIG)
        self.cfg_path = os.path.join(args.home, "config.json")
        try:
            with open(self.cfg_path) as f:
                self.cfg.update({k: v for k, v in json.load(f).items() if k in CONFIG})
        except (OSError, ValueError):
            pass
        self.follow_until = 0.0     # until then a phrase is a question without the wake word
        self.deaf_until = 0.0       # do not take our own voice or beep for a question
        self.hearing = False        # the always-on listener is running
        self.cancel = False
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
        self.wake = None
        if p.whisper_server and p.wake_model:
            self.wake = Server("whisper-wake", [p.whisper_server, "-m", p.wake_model, "-l", "it", "-t", "2",
                                                "--host", "127.0.0.1", "--port", str(args.stt_port + 2),
                                                "--prompt", PROMPT],
                               "http://127.0.0.1:%d/" % (args.stt_port + 2))
        if p.llama_server and p.llm_model:
            self.llm = Server("llama-server", [p.llama_server, "-m", p.llm_model, "-c", "2048", "-t", "4",
                                               "--host", "127.0.0.1", "--port", str(args.llm_port)],
                              "http://127.0.0.1:%d/health" % args.llm_port)
        threading.Thread(target=self._watchdog, daemon=True).start()
        self.phrases = queue.Queue(maxsize=2)
        threading.Thread(target=self._ear, name="ear", daemon=True).start()
        threading.Thread(target=self._phrases, name="phrases", daemon=True).start()
        threading.Thread(target=self._course, name="course", daemon=True).start()

    def set_config(self, form):
        c = dict(self.cfg)
        for k in ("always", "course"):
            if k in form:
                c[k] = form[k] in ("1", "true", "on")
        if form.get("wake", "").strip():
            c["wake"] = " ".join(form["wake"].split())[:40]
        if "courseDeg" in form:
            try:
                c["courseDeg"] = max(5, min(90, int(float(form["courseDeg"]))))
            except ValueError:
                pass
        self.cfg = c
        try:
            with open(self.cfg_path + ".tmp", "w") as f:
                json.dump(c, f)
            os.replace(self.cfg_path + ".tmp", self.cfg_path)
        except OSError as e:
            log.warning("config: %s", e)

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

    def transcribe(self, wav, quick=False):
        port = self.args.stt_port
        if quick and self.wake and self.wake.ok:
            port += 2
        elif not (self.stt and self.stt.ok):
            raise RuntimeError("whisper-server not ready")
        with open(wav, "rb") as f:
            body, ctype = multipart({"response_format": "json", "temperature": "0.0"}, {"file": ("q.wav", f.read())})
        req = urllib.request.Request("http://127.0.0.1:%d/inference" % port, data=body,
                                     headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.loads(r.read()).get("text", "")
        text = " ".join(text.split())
        # Whisper writes these for silence
        if text.strip(" .").lower() in ("", "[blank_audio]", "sottotitoli a cura di qtss", "grazie per la visione") \
                or re.fullmatch(r"[\[(].*[\])]", text.strip()):      # [Musica], (rumore)
            return ""
        return text

    def node(self, path, form=None):
        url = self.args.node + path
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5) as r:
            return json.loads(r.read() or b"null")

    def node_data(self, parts=("state", "messages", "userdata")):
        data = {}
        for k in parts:
            try:
                data[k] = self.node("/api/" + k)
            except Exception as e:
                log.warning("node /api/%s: %s", k, e)
        return data

    def _answer(self, text):
        self.heard = text
        data = self.node_data()
        reply, action = assistant.answer(text, data)
        llm = self.llm and self.llm.ok
        if llm and action is None and len(text.split()) > 8:
            reply = None                # a long question is more than a command: the language model has it
        if reply is None and llm and text:
            self._done(text, self.ask_llm(text, data))
            return
        if reply is None:
            reply = "Non ho capito. " + assistant.HELP
        if action and action[0] == "send":
            try:
                self.node("/api/messages", {"to": action[1], "name": action[2], "text": action[3]})
            except Exception as e:
                reply = "Il messaggio non è partito: il nodo non risponde."
                log.warning("send: %s", e)
        self._done(text, reply)

    def ask_llm(self, text, data):
        """The answer of the language model, a sentence at a time as it comes."""
        body = json.dumps({"messages": assistant.llm_messages(text, data, history=self.history), "max_tokens": 200,
                           "temperature": 0.5, "stream": True, "cache_prompt": True}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % self.args.llm_port, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                def chunks():
                    for line in r:
                        line = line.decode("utf-8", "replace").strip()
                        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                            continue
                        try:
                            yield json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
                        except (ValueError, KeyError, IndexError):
                            pass
                got = False
                for sent in sentences(chunks()):
                    sent = " ".join(sent.replace("*", "").replace("#", "").split())
                    if sent:
                        got = True
                        yield sent
                if not got:
                    yield "Non so cosa rispondere."
        except Exception as e:
            log.warning("language model: %s", e)
            yield "Il modello linguistico non risponde."

    def _done(self, heard, reply, error=None):
        """Says the answer: a text, or the sentences of the language model as they come."""
        parts = [reply] if isinstance(reply, str) else reply
        with self.lock:
            self.heard, self.reply, self.error = heard, "", error
            self.cancel = False
        said = []
        for sent in parts:
            if self.cancel:
                break
            said.append(sent)
            with self.lock:
                self.reply = " ".join(said)
                self.state = "speaking"
            try:
                self.speak(sent)
            except Exception as e:
                log.warning("speak: %s", e)
                self.error = "Piper: %s" % e
        reply = " ".join(said)
        log.info("Q: %s | A: %s", heard, reply)
        with self.lock:
            self.reply = reply
            if heard:
                self.history = (self.history + [(heard, reply)])[-10:]
            if self.state in ("speaking", "thinking"):
                self.state = "idle"
            self.deaf_until = time.monotonic() + 0.5
            if heard and self.cfg["always"]:
                self.follow_until = time.monotonic() + FOLLOW_S

    def announce(self, text):
        """Says something nobody asked (off course): only when nothing else is going on. False if it could not."""
        with self.lock:
            if self.state != "idle":
                return False
            self.state = "speaking"
        threading.Thread(target=self._done, args=("", text), daemon=True).start()
        return True

    # ---- listening all the time ----

    def _ear(self):
        while True:
            if not (self.cfg["always"] and self.parts.stream and self.stt):
                self.hearing = False
                time.sleep(1)
                continue
            cmd = [self.parts.stream, "--rate", str(RATE), "--channels", "1"]
            cmd += ["--format=s16le", "--raw"] if self.parts.stream == "parecord" else ["--format", "s16", "-"]
            log.info("listening all the time (%s)", self.parts.stream)
            err = tempfile.TemporaryFile()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
            self.hearing = True
            try:
                self._cut(proc.stdout)
            except Exception as e:
                log.warning("listening: %s", e)
            finally:
                proc.kill()
                proc.wait()
                self.hearing = False
                err.seek(0)
                msg = err.read().decode("utf-8", "replace").strip()
                err.close()
            if self.cfg["always"]:
                log.warning("the recorder stopped%s", (": " + msg[-300:]) if msg else "")
                time.sleep(2)               # the recorder stopped (the headset went away): again in a moment

    def _cut(self, stream):
        """Cuts the audio into phrases: louder than the background for 90 ms starts one, 0,8 s of quiet ends it."""
        floor = None
        pre = collections.deque(maxlen=10)
        seg, loud, quiet = None, 0, 0
        report, loudest, cut = time.monotonic() + 5, 0.0, 0
        while self.cfg["always"]:
            b = stream.read(FRAME * 2)
            if len(b) < FRAME * 2:
                return
            if time.monotonic() > report:   # in the log, to set the microphone's volume
                log.info("microphone: background %d, loudest %d, start of a phrase above %d; %d phrases",
                         floor or 0, loudest, max(MIN_RMS, (floor or 0) * 3), cut)
                report, loudest, cut = time.monotonic() + 60, 0.0, 0
            busy = self.state != "idle" or time.monotonic() < self.deaf_until
            if busy:
                seg, loud = None, 0
                pre.clear()
                continue
            r = rms(b)
            loudest = max(loudest, r)
            if seg is None:                 # the background noise, followed slowly up and quickly down
                floor = r if floor is None else floor + (r - floor) * (0.1 if r < floor else 0.01)
            thr = max(MIN_RMS, floor * 3)
            if seg is None:
                pre.append(b)
                loud = loud + 1 if r > thr else 0
                if loud >= 3:
                    seg, quiet = list(pre), 0
                    pre.clear()
                continue
            seg.append(b)
            quiet = quiet + 1 if r < thr else 0
            if quiet >= 27 or len(seg) >= 400:          # 0,8 s of quiet, or 12 s
                if len(seg) - quiet >= 12:
                    cut += 1
                    try:
                        self.phrases.put_nowait(b"".join(seg))
                    except queue.Full:
                        pass
                seg, loud = None, 0

    def _phrases(self):
        while True:
            self.heard_phrase(self.phrases.get())

    def heard_phrase(self, pcm):
        if self.state != "idle":
            return
        wav = os.path.join(self.tmp, "phrase.wav")
        write_wav(wav, pcm)
        following = time.monotonic() < self.follow_until
        try:
            text = self.transcribe(wav, quick=not following)
        except Exception as e:
            log.warning("speech to text: %s", e)
            return
        if not text:
            return
        if following:
            question = text
        else:
            rest = after_wake(text, self.cfg["wake"])
            if rest is None:
                log.info("heard, not for us: %s", text)
                return
            if len(rest.split()) < 2:
                self.follow_until = time.monotonic() + FOLLOW_S
                self._play(self.beep)
                self.deaf_until = time.monotonic() + 0.3
                return
            if self.wake and self.wake.ok:          # heard by the small model: the big one writes it better
                try:
                    text = self.transcribe(wav) or text
                except Exception:
                    pass
            question = strip_wake(text, self.cfg["wake"])
        with self.lock:
            if self.state != "idle":
                return
            self.state, self.heard, self.reply, self.error = "thinking", question, "", None
            self.follow_until = 0
        self._answer(question)

    # ---- off course ----

    def _course(self):
        errs = collections.deque(maxlen=COURSE_SAMPLES)
        last, warned = 0.0, False
        while True:
            time.sleep(COURSE_EVERY_S)
            c = assistant.course_error(self.node_data(("state", "userdata"))) if self.cfg["course"] else None
            if c is None:
                errs.clear()
                warned = False
                continue
            errs.append(c[0])
            mean = sum(errs) / len(errs)
            lim = self.cfg["courseDeg"]
            if len(errs) == COURSE_SAMPLES and abs(mean) > lim and time.monotonic() - last > COURSE_REPEAT_S:
                if self.announce(assistant.off_course_text(mean, c[1], c[2], c[3])):
                    last, warned = time.monotonic(), True
            elif warned and abs(mean) < lim * 0.6:
                if self.announce("Di nuovo in rotta verso %s." % c[3]):
                    warned, last = False, 0.0

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
        self.cancel = True
        self.follow_until = 0
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
                "llm": bool(self.llm and self.llm.ok), "mic": bool(p.record), "config": self.cfg,
                "hearing": self.hearing, "follow": time.monotonic() < self.follow_until,
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
            elif path == "/config":
                v.set_config(form)
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
