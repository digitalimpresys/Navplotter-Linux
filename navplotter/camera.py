"""python3 -m navplotter.camera: live video from a USB or network camera, with objects recognised by a Hailo AI accelerator.

A separate service from the node, so that the radio keeps working whatever happens to the camera. It listens only
on 127.0.0.1; the node's web server passes /camera/... to it, so the web app reaches it from the same address.

  GET  /stream.mjpg   the video (MJPEG), with the boxes of the recognised objects drawn on it
  GET  /status        JSON: camera, detector, fps, objects seen in the last frame
  POST /settings      night=0|1 (brighter, more contrast in the dark), detect=0|1, record=0|1
  GET  /recordings                the recordings: folders of frames, one a second, to train a model of our own
  GET  /recordings/<name>.zip     one recording as a zip of JPEG files
  POST /recordings/delete         name=<name>
  GET  /config        the camera to use: {"source": "usb"|"ip", "host", "user", "stream", "hasPassword"}
  POST /config        source, host, user, password (left empty: unchanged), stream=stream1|stream2
  POST /ptz           move=left|right|up|down|stop: pan and tilt of a network camera (ONVIF), sent again every
                      half second while a button is held; the camera stops by itself a second after the last one

A network camera (Tapo C510W and the like) gives its video over RTSP and is turned over ONVIF, both with the camera
account made in the Tapo app. Its settings are kept in /var/lib/navplotter/camera.json, readable only by the service.

Without the Hailo (or without its software) the video still works, without recognition.

The model: a .hef in /var/lib/navplotter/models (a model trained by us, see training/README.md) wins over the
models of the hailo-models package. Its classes are in a .json file with the same name:
  {"names": ["boat", "ship", ...], "it": {"boat": "barca", ...}, "important": ["boat", "person"]}
Without it the classes are the 80 COCO ones.
"""
import argparse
import glob
import queue
import json
import logging
import os
import threading
import time
import shutil
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("navplotter.camera")

# COCO classes, as the YOLO models of the Hailo examples are trained on
COCO = ("person bicycle car motorcycle airplane bus train truck boat traffic-light fire-hydrant stop-sign "
        "parking-meter bench bird cat dog horse sheep cow elephant bear zebra giraffe backpack umbrella handbag tie "
        "suitcase frisbee skis snowboard sports-ball kite baseball-bat baseball-glove skateboard surfboard "
        "tennis-racket bottle wine-glass cup fork knife spoon bowl banana apple sandwich orange broccoli carrot "
        "hot-dog pizza donut cake chair couch potted-plant bed dining-table toilet tv laptop mouse remote keyboard "
        "cell-phone microwave oven toaster sink refrigerator book clock vase scissors teddy-bear hair-drier "
        "toothbrush").split()
LABELS_IT = {"person": "persona", "boat": "barca", "bird": "uccello", "surfboard": "tavola", "kite": "aquilone",
             "sports-ball": "palla", "bottle": "bottiglia", "airplane": "aereo", "dog": "cane"}
# What matters at sea: drawn in red
IMPORTANT = {"person", "boat"}

DATA = "/var/lib/navplotter"
DETECTOR_RETRY_S = 30.0
REC_EVERY_S = 1.0           # one frame a second: enough variety for training, and not too many alike
REC_MAX_FRAMES = 3600       # a recording stops by itself after an hour
REC_MIN_FREE = 500 << 20    # and when less than 500 MB are left on the disk
RTSP_PORT, ONVIF_PORT = 554, 2020        # the ports of the Tapo cameras
PTZ_SPEED = 0.5
PTZ_MOVES = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}
CONFIG_DEFAULT = {"source": "usb", "host": "", "user": "", "password": "", "stream": "stream1"}


class Labels:
    """Class names of the model, their Italian names and the ones that matter at sea (drawn in red)."""

    def __init__(self, names=COCO, it=None, important=None):
        self.names = list(names)
        self.it = dict(LABELS_IT if it is None else it)
        self.important = set(IMPORTANT if important is None else important)

    @classmethod
    def for_model(cls, hef):
        path = os.path.splitext(hef)[0] + ".json" if hef else None
        if not path or not os.path.exists(path):
            return cls()
        with open(path) as f:
            j = json.load(f)
        return cls(j["names"], j.get("it", {}), j.get("important", []))

    def name(self, label, lang="it"):
        return self.it.get(label, label) if lang == "it" else label


HEF_PATHS = ["/usr/share/hailo-models/yolov8m_h8.hef", "/usr/share/hailo-models/yolov8s_h8.hef",
             "/usr/share/hailo-models/yolov11s_h8.hef", "/usr/share/hailo-models/yolov8s_h8l.hef",
             "/usr/share/hailo-models/yolov6n_h8.hef", "/usr/share/hailo-models/yolov8s.hef"]


class HailoDetector:
    """YOLO with NMS on the Hailo (the models of the hailo-models package). detect(rgb) -> [(label, score, box)],
    box as (x0, y0, x1, y1) in 0..1 of the frame."""

    def __init__(self, hef, labels=None):
        import numpy as np
        self.labels = labels or Labels()
        from hailo_platform import VDevice, FormatType, HailoSchedulingAlgorithm
        self.np = np
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self.vdevice = VDevice(params)
        self.model = self.vdevice.create_infer_model(hef)
        self.model.output().set_format_type(FormatType.FLOAT32)
        self.configured = self.model.configure().__enter__()
        self.h, self.w = self.model.input().shape[:2]
        self.out_shape = self.model.output().shape
        self.name = os.path.basename(hef)

    def detect(self, rgb, min_score=0.4):
        np = self.np
        import cv2
        fh, fw = rgb.shape[:2]
        # Letterbox: the frame scaled into the model input without changing its proportions
        k = min(self.w / fw, self.h / fh)
        nw, nh = int(fw * k), int(fh * k)
        px, py = (self.w - nw) // 2, (self.h - nh) // 2
        inp = np.full((self.h, self.w, 3), 114, dtype=np.uint8)
        inp[py:py + nh, px:px + nw] = cv2.resize(rgb, (nw, nh))
        bindings = self.configured.create_bindings()
        bindings.input().set_buffer(inp)
        out = np.empty(self.out_shape, dtype=np.float32)
        bindings.output().set_buffer(out)
        self.configured.run([bindings], 2000)
        return self._parse(bindings.output().get_buffer(), fw, fh, k, px, py, min_score)

    def per_class(self, result):
        """The NMS result as one list of (y0, x0, y1, x1, score) per class, 0..1 of the input. Depending on the
        HailoRT version it comes as a list of arrays per class, or as one flat buffer: for each class the number
        of boxes, then 5 values each."""
        np = self.np
        if isinstance(result, (list, tuple)):
            return [np.asarray(c).reshape(-1, 5) for c in result]
        flat, i, classes = np.asarray(result).reshape(-1), 0, []
        while i < len(flat) and len(classes) < len(self.labels.names):
            n = int(flat[i])
            classes.append(flat[i + 1:i + 1 + 5 * n].reshape(-1, 5))
            i += 1 + 5 * n
        return classes

    def _parse(self, result, fw, fh, k, px, py, min_score):
        found = []
        for cls, boxes in enumerate(self.per_class(result)):
            for y0, x0, y1, x1, score in boxes:
                if score < min_score or cls >= len(self.labels.names):
                    continue
                box = tuple(min(1.0, max(0.0, float(v))) for v in (
                    (x0 * self.w - px) / k / fw, (y0 * self.h - py) / k / fh,
                    (x1 * self.w - px) / k / fw, (y1 * self.h - py) / k / fh))
                found.append((self.labels.names[cls], float(score), box))
        return found


def find_hef(setting, models_dir=DATA + "/models"):
    if setting and setting != "auto":
        return setting if os.path.exists(setting) else None
    own = sorted(glob.glob(os.path.join(models_dir, "*.hef")), key=os.path.getmtime)
    if own:
        return own[-1]          # our own model, the newest one
    for p in HEF_PATHS:
        if os.path.exists(p):
            return p
    found = sorted(glob.glob("/usr/share/hailo-models/yolo*.hef"))
    return found[0] if found else None


def load_config(path):
    c = dict(CONFIG_DEFAULT)
    try:
        with open(path) as f:
            c.update({k: v for k, v in json.load(f).items() if k in CONFIG_DEFAULT})
    except (OSError, ValueError):
        pass
    return c


def save_config(path, c):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # it holds the camera password
    with os.fdopen(fd, "w") as f:
        json.dump(c, f)
    os.replace(tmp, path)


def rtsp_url(c):
    auth = ""
    if c["user"]:
        q = lambda v: urllib.parse.quote(v, safe="")
        auth = "%s:%s@" % (q(c["user"]), q(c["password"]))
    return "rtsp://%s%s:%d/%s" % (auth, c["host"], RTSP_PORT, c["stream"] or "stream1")


class LatestFrame:
    """Reads a network stream in a thread of its own and keeps only the newest frame: when recognition is slower
    than the camera, the frames would otherwise pile up and the video would fall behind more and more."""

    def __init__(self, cap):
        self.cap = cap
        self.frames = queue.Queue(maxsize=1)
        self.running = True
        self.thread = threading.Thread(target=self._run, name="rtsp", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while self.running:
                ok, frame = self.cap.read()
                if not ok:
                    frame = None
                try:
                    self.frames.get_nowait()        # the older frame, not looked at: thrown away
                except queue.Empty:
                    pass
                self.frames.put(frame)
                if frame is None:
                    break
        finally:
            # Released here, by the thread that reads: releasing it while a read is under way crashes FFmpeg
            self.cap.release()

    def read(self):
        try:
            frame = self.frames.get(timeout=10)
        except queue.Empty:
            return False, None
        return frame is not None, frame

    def set(self, *a):
        return self.cap.set(*a)

    def release(self):
        self.running = False
        self.thread.join(timeout=15)


def find_camera(setting):
    if setting not in ("", "auto", None):
        return setting
    # USB cameras only: on the Pi 5 the /dev/video* of the chip itself (pispbe, hevc) are not cameras
    return sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0"))


class Camera:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Condition()
        self.jpeg = None
        self.frame_no = 0
        self.objects = []
        self.fps = 0.0
        self.device = None
        self.error = "starting"
        self.night = False
        self.detect_on = True
        self.detector = None
        self.detector_error = None
        self.detector_tried = 0.0  # when loading the model was last tried (monotonic)
        self.labels = Labels()
        self.clients = 0
        self.rec_dir = os.path.join(args.data, "recordings")
        self.recording = None       # name of the recording in progress
        self.rec_frames = 0
        self.rec_last = 0.0
        self.config_path = os.path.join(args.data, "camera.json")
        self.config = load_config(self.config_path)
        self.reopen = False         # set when the settings change: the camera is opened again
        self.ptz = None
        self.ptz_error = None
        self.ptz_lock = threading.Lock()
        self._ptz_connect()

    def start(self):
        threading.Thread(target=self._run, name="camera", daemon=True).start()

    def _open(self, cv2):
        c = self.config
        if c["source"] == "ip":
            if not c["host"]:
                raise RuntimeError("network camera without an address")
            # TCP: over WiFi UDP loses pieces of the frames (grey smears)
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|timeout;8000000")
            cap = cv2.VideoCapture(rtsp_url(c), cv2.CAP_FFMPEG)
            if not cap.isOpened():
                cap.release()
                raise RuntimeError("no video from %s (address, camera account, privacy mode off?)" % c["host"])
            return "%s %s" % (c["host"], c["stream"]), LatestFrame(cap)
        candidates = find_camera(self.args.device)
        if isinstance(candidates, str):
            candidates = [candidates]
        for dev in candidates:
            cap = cv2.VideoCapture(dev, cv2.CAP_V4L2 if dev.startswith("/dev/") else cv2.CAP_ANY)
            if not cap.isOpened():
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.args.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.args.height)
            ok, _ = cap.read()
            if ok:
                return dev, cap
            cap.release()
        raise RuntimeError("no USB camera found")

    def _load_detector(self):
        self.detector_tried = time.monotonic()
        hef = find_hef(self.args.hef, os.path.join(self.args.data, "models"))
        if hef is None:
            self.detector_error = "no Hailo model (.hef) found: sudo apt install hailo-all"
            return
        try:
            self.labels = Labels.for_model(hef)
            self.detector = HailoDetector(hef, self.labels)
            self.detector_error = None
            log.info("Hailo detector ready: %s (%d classes)", hef, len(self.labels.names))
        except Exception as e:
            error = "Hailo: %s" % e
            if error != self.detector_error:     # tried again every DETECTOR_RETRY_S: logged once
                log.warning("no recognition: %s", error)
            self.detector_error = error

    def _run(self):
        import cv2
        self._load_detector()
        last_error = None
        while True:
            try:
                self.reopen = False
                self.device, cap = self._open(cv2)
                log.info("camera %s", self.device)
                self.error = None
                self._loop(cv2, cap)
            except Exception as e:
                self.error = str(e)
                if self.error != last_error:
                    log.warning("camera: %s", e)
                    last_error = self.error
            self.device = None
            time.sleep(3)

    def _loop(self, cv2, cap):
        t0, n = time.monotonic(), 0
        try:
            while True:
                if self.reopen:
                    self.reopen = False
                    return
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError("the camera stopped giving frames")
                if self.recording:
                    self._record(cv2, frame)
                if self.night:
                    frame = night_view(cv2, frame)
                objects = []
                if (self.detector is None and self.detect_on
                        and time.monotonic() - self.detector_tried > DETECTOR_RETRY_S):
                    self._load_detector()       # the Hailo may have been busy (another program) or come back
                if self.detector and self.detect_on:
                    try:
                        objects = self.detector.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), self.args.min_score)
                    except Exception as e:
                        self.detector_error = "Hailo: %s" % e
                        log.warning("recognition stopped: %s", e)
                        self.detector = None
                draw(cv2, frame, objects, self.labels)
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.args.quality])
                with self.lock:
                    self.jpeg, self.objects = jpg.tobytes(), objects
                    self.frame_no += 1
                    self.lock.notify_all()
                n += 1
                if time.monotonic() - t0 >= 2:
                    self.fps, t0, n = n / (time.monotonic() - t0), time.monotonic(), 0
        finally:
            cap.release()

    # ---- Settings and pan/tilt of a network camera ----

    def config_public(self):
        c = self.config
        return {"source": c["source"], "host": c["host"], "user": c["user"], "stream": c["stream"],
                "hasPassword": bool(c["password"])}

    def set_config(self, form):
        c = dict(self.config)
        if form.get("source") in ("usb", "ip"):
            c["source"] = form["source"]
        if "host" in form:
            c["host"] = "".join(ch for ch in form["host"].strip() if ch.isalnum() or ch in ".-:[]")
        if "user" in form:
            c["user"] = form["user"].strip()
        if form.get("password"):
            c["password"] = form["password"]
        if form.get("stream") in ("stream1", "stream2"):
            c["stream"] = form["stream"]
        save_config(self.config_path, c)
        self.config = c
        self.reopen = True
        self.error = "starting"
        log.info("camera settings: %s %s", c["source"], c["host"])
        self._ptz_connect()

    def _ptz_connect(self):
        c = self.config
        self.ptz, self.ptz_error = None, None
        if c["source"] != "ip" or not c["host"]:
            return
        from .onvif import Ptz, OnvifError

        def run():
            p = Ptz(c["host"], ONVIF_PORT, c["user"], c["password"])
            try:
                p.connect()
            except OnvifError as e:
                if c is self.config:
                    self.ptz_error = str(e)
                log.warning("no pan/tilt: %s", e)
                return
            if c is self.config:
                self.ptz = p
                log.info("pan/tilt on %s, profile %s", c["host"], p.profile)
        threading.Thread(target=run, name="onvif", daemon=True).start()

    def ptz_move(self, move):
        p = self.ptz
        if p is None:
            return self.ptz_error or "no pan/tilt"
        from .onvif import OnvifError
        try:
            with self.ptz_lock:
                if move == "stop":
                    p.stop()
                elif move in PTZ_MOVES:
                    x, y = PTZ_MOVES[move]
                    p.move(x * PTZ_SPEED, y * PTZ_SPEED)
        except OnvifError as e:
            log.warning("pan/tilt: %s", e)
            return str(e)
        return None

    # ---- Recordings: the frames as the camera gives them (no boxes, no night view), to label and train on ----

    def start_recording(self):
        if self.recording:
            return
        name = time.strftime("%Y%m%d-%H%M%S")
        os.makedirs(os.path.join(self.rec_dir, name), exist_ok=True)
        self.recording, self.rec_frames, self.rec_last = name, 0, 0.0
        log.info("recording %s", name)

    def stop_recording(self):
        if self.recording:
            log.info("recording %s stopped, %d frames", self.recording, self.rec_frames)
        self.recording = None

    def _record(self, cv2, frame):
        now = time.monotonic()
        if now - self.rec_last < REC_EVERY_S:
            return
        self.rec_last = now
        folder = os.path.join(self.rec_dir, self.recording)
        if self.rec_frames >= REC_MAX_FRAMES or shutil.disk_usage(self.rec_dir).free < REC_MIN_FREE:
            self.stop_recording()
            return
        self.rec_frames += 1
        cv2.imwrite(os.path.join(folder, "%s-%05d.jpg" % (self.recording, self.rec_frames)), frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])

    def recordings(self):
        out = []
        if os.path.isdir(self.rec_dir):
            for name in sorted(os.listdir(self.rec_dir), reverse=True):
                folder = os.path.join(self.rec_dir, name)
                if not os.path.isdir(folder):
                    continue
                files = [f for f in os.listdir(folder) if f.endswith(".jpg")]
                size = sum(os.path.getsize(os.path.join(folder, f)) for f in files)
                out.append({"name": name, "frames": len(files), "mb": round(size / 1e6, 1),
                            "recording": name == self.recording})
        return out

    def rec_path(self, name):
        """The folder of a recording, or None: names are only the ones we make (digits and a dash)."""
        if not name or not all(c.isdigit() or c == "-" for c in name):
            return None
        folder = os.path.join(self.rec_dir, name)
        return folder if os.path.isdir(folder) else None

    def status(self):
        with self.lock:
            objs = [{"label": l, "name": self.labels.name(l), "score": round(s, 2), "box": [round(v, 3) for v in b],
                     "important": l in self.labels.important} for l, s, b in self.objects]
        return {"ok": self.error is None and self.jpeg is not None, "device": self.device, "error": self.error,
                "fps": round(self.fps, 1), "night": self.night, "detect": self.detect_on,
                "detector": self.detector.name if self.detector else None, "detectorError": self.detector_error,
                "objects": objs, "record": bool(self.recording), "source": self.config["source"],
                "ptz": self.ptz is not None, "ptzError": self.ptz_error,
                "recording": {"name": self.recording, "frames": self.rec_frames} if self.recording else None}


def night_view(cv2, frame):
    """Brighter and with more contrast in the dark (CLAHE on the luminance)."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def draw(cv2, frame, objects, labels):
    h, w = frame.shape[:2]
    for label, score, (x0, y0, x1, y1) in objects:
        color = (40, 40, 255) if label in labels.important else (60, 220, 255)
        p0, p1 = (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h))
        cv2.rectangle(frame, p0, p1, color, 2)
        text = "%s %d%%" % (labels.name(label), score * 100)
        cv2.putText(frame, text, (p0[0] + 3, max(16, p0[1] - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)


def make_handler(cam):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NavPlotterCamera"

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
            path = urllib.parse.urlparse(self.path).path
            if path == "/status":
                self._json(cam.status())
            elif path == "/stream.mjpg":
                self._stream()
            elif path == "/config":
                self._json(cam.config_public())
            elif path == "/recordings":
                self._json({"recordings": cam.recordings()})
            elif path.startswith("/recordings/") and path.endswith(".zip"):
                self._zip(path[len("/recordings/"):-4])
            elif path == "/snapshot.jpg":
                with cam.lock:
                    jpg = cam.jpeg
                if jpg is None:
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpg)))
                self.end_headers()
                self.wfile.write(jpg)
            else:
                self.send_error(404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[-1] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode() if n else "").items()}
            path = urllib.parse.urlparse(self.path).path
            if path == "/recordings/delete":
                folder = cam.rec_path(form.get("name"))
                if folder and form.get("name") != cam.recording:
                    shutil.rmtree(folder, ignore_errors=True)
                self._json({"recordings": cam.recordings()})
                return
            if path == "/config":
                try:
                    cam.set_config(form)
                except OSError as e:
                    self._json({"ok": False, "error": str(e)})
                    return
                self._json(dict(cam.config_public(), ok=True))
                return
            if path == "/ptz":
                error = cam.ptz_move(form.get("move", "stop"))
                self._json({"ok": error is None, "error": error})
                return
            if path != "/settings":
                self.send_error(404)
                return
            if "record" in form:
                try:
                    cam.start_recording() if form["record"] == "1" else cam.stop_recording()
                except OSError as e:
                    log.warning("cannot record: %s", e)
            if "night" in form:
                cam.night = form["night"] == "1"
            if "detect" in form:
                cam.detect_on = form["detect"] == "1"
            self._json(cam.status())

        def _zip(self, name):
            folder = cam.rec_path(name)
            if folder is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="navplotter-%s.zip"' % name)
            self.end_headers()
            try:
                # JPEG files are already compressed: stored as they are, written while the zip is sent
                with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:
                    for f in sorted(os.listdir(folder)):
                        if f.endswith(".jpg"):
                            z.write(os.path.join(folder, f), "%s/%s" % (name, f))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            cam.clients += 1
            try:
                while True:
                    with cam.lock:
                        cam.lock.wait_for(lambda: cam.frame_no != seen, timeout=5)
                        if cam.frame_no == seen or cam.jpeg is None:
                            continue
                        jpg, seen = cam.jpeg, cam.frame_no
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                cam.clients -= 1

    return Handler


def main():
    ap = argparse.ArgumentParser(prog="navplotter.camera", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--device", default="auto", help="/dev/videoN, or auto")
    ap.add_argument("--hef", default="auto", help="Hailo model (.hef), or auto")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--min-score", type=float, default=0.4)
    ap.add_argument("--data", default=DATA, help="folder of the recordings and of our own models")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cam = Camera(args)
    cam.start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cam))
    server.daemon_threads = True
    log.info("camera service on 127.0.0.1:%d", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
