"""python3 -m navplotter.camera: live video from a USB camera, with objects recognised by a Hailo AI accelerator.

A separate service from the node, so that the radio keeps working whatever happens to the camera. It listens only
on 127.0.0.1; the node's web server passes /camera/... to it, so the web app reaches it from the same address.

  GET  /stream.mjpg   the video (MJPEG), with the boxes of the recognised objects drawn on it
  GET  /status        JSON: camera, detector, fps, objects seen in the last frame
  POST /settings      night=0|1 (brighter, more contrast in the dark), detect=0|1

Without the Hailo (or without its software) the video still works, without recognition.
"""
import argparse
import glob
import json
import logging
import os
import threading
import time
import urllib.parse
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

HEF_PATHS = ["/usr/share/hailo-models/yolov8m_h8.hef", "/usr/share/hailo-models/yolov8s_h8.hef",
             "/usr/share/hailo-models/yolov11s_h8.hef", "/usr/share/hailo-models/yolov8s_h8l.hef",
             "/usr/share/hailo-models/yolov6n_h8.hef", "/usr/share/hailo-models/yolov8s.hef"]


class HailoDetector:
    """YOLO with NMS on the Hailo (the models of the hailo-models package). detect(rgb) -> [(label, score, box)],
    box as (x0, y0, x1, y1) in 0..1 of the frame."""

    def __init__(self, hef):
        import numpy as np
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
        while i < len(flat) and len(classes) < len(COCO):
            n = int(flat[i])
            classes.append(flat[i + 1:i + 1 + 5 * n].reshape(-1, 5))
            i += 1 + 5 * n
        return classes

    def _parse(self, result, fw, fh, k, px, py, min_score):
        found = []
        for cls, boxes in enumerate(self.per_class(result)):
            for y0, x0, y1, x1, score in boxes:
                if score < min_score or cls >= len(COCO):
                    continue
                box = tuple(min(1.0, max(0.0, float(v))) for v in (
                    (x0 * self.w - px) / k / fw, (y0 * self.h - py) / k / fh,
                    (x1 * self.w - px) / k / fw, (y1 * self.h - py) / k / fh))
                found.append((COCO[cls], float(score), box))
        return found


def find_hef(setting):
    if setting and setting != "auto":
        return setting if os.path.exists(setting) else None
    for p in HEF_PATHS:
        if os.path.exists(p):
            return p
    found = sorted(glob.glob("/usr/share/hailo-models/yolo*.hef"))
    return found[0] if found else None


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
        self.clients = 0

    def start(self):
        threading.Thread(target=self._run, name="camera", daemon=True).start()

    def _open(self, cv2):
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
        hef = find_hef(self.args.hef)
        if hef is None:
            self.detector_error = "no Hailo model (.hef) found: sudo apt install hailo-all"
            return
        try:
            self.detector = HailoDetector(hef)
            log.info("Hailo detector ready: %s", hef)
        except Exception as e:
            self.detector_error = "Hailo: %s" % e
            log.warning("no recognition: %s", self.detector_error)

    def _run(self):
        import cv2
        self._load_detector()
        last_error = None
        while True:
            try:
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
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError("the camera stopped giving frames")
                if self.night:
                    frame = night_view(cv2, frame)
                objects = []
                if self.detector and self.detect_on:
                    try:
                        objects = self.detector.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), self.args.min_score)
                    except Exception as e:
                        self.detector_error = "Hailo: %s" % e
                        log.warning("recognition stopped: %s", e)
                        self.detector = None
                draw(cv2, frame, objects)
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

    def status(self):
        with self.lock:
            objs = [{"label": l, "name": LABELS_IT.get(l, l), "score": round(s, 2), "box": [round(v, 3) for v in b],
                     "important": l in IMPORTANT} for l, s, b in self.objects]
        return {"ok": self.error is None and self.jpeg is not None, "device": self.device, "error": self.error,
                "fps": round(self.fps, 1), "night": self.night, "detect": self.detect_on,
                "detector": self.detector.name if self.detector else None, "detectorError": self.detector_error,
                "objects": objs}


def night_view(cv2, frame):
    """Brighter and with more contrast in the dark (CLAHE on the luminance)."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def draw(cv2, frame, objects):
    h, w = frame.shape[:2]
    for label, score, (x0, y0, x1, y1) in objects:
        color = (40, 40, 255) if label in IMPORTANT else (60, 220, 255)
        p0, p1 = (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h))
        cv2.rectangle(frame, p0, p1, color, 2)
        text = "%s %d%%" % (LABELS_IT.get(label, label), score * 100)
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
            if urllib.parse.urlparse(self.path).path != "/settings":
                self.send_error(404)
                return
            if "night" in form:
                cam.night = form["night"] == "1"
            if "detect" in form:
                cam.detect_on = form["detect"] == "1"
            self._json(cam.status())

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
