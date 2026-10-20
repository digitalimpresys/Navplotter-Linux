"""python3 -m unittest discover tests"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from navplotter import ais as A
from navplotter import chat as C
from navplotter import protocol as P
from navplotter.mesh import SeenTable
from navplotter.node import Node
from navplotter.settings import Settings
from navplotter.traffic import AlarmSettings, Own, Traffic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeModem:
    ready = True

    def __init__(self):
        self.sent = []

    def transmit(self, data):
        self.sent.append(data)
        return True


def make_node(tmp, **extra):
    s = Settings(os.path.join(tmp, "settings.json"))
    s["data_dir"] = tmp
    s["ais"]["destinations"] = ["127.0.0.1:9"]
    for k, v in extra.items():
        s[k] = v
    clock = Clock()
    node = Node(s, clock)
    node.modem = FakeModem()
    node.on_modem_ready(0xB8DF802C)
    return node, clock


class Packets(unittest.TestCase):
    def test_position_round_trip(self):
        pos = P.Position(2, 0xA7C94F1B, 39, 1 << 1, 45.46366, 12.14712, 161.3, 4.25, "Gejsha")
        data = pos.pack()
        self.assertEqual(len(data), 34)
        self.assertEqual(data[:4], b"NP\x01\x02")
        back = P.unpack_position(data)
        self.assertEqual((back.id, back.seq, back.name, back.node_type), (0xA7C94F1B, 39, "Gejsha", P.NODE_PERSON))
        self.assertAlmostEqual(back.lat, 45.46366, places=6)
        self.assertAlmostEqual(back.cog, 161.3)
        self.assertAlmostEqual(back.sog, 4.25)

    def test_name_is_cleaned(self):
        data = P.Position(2, 1, 0, 0, 0, 0, 0, 0, "x").pack()
        data = data[:22] + b'A"<b>\\cdefghijk'[:12]
        self.assertEqual(P.unpack_position(data).name, "A??b??cdefg")

    def test_message_lengths(self):
        conf = P.Message(2, 1, 5, P.MSG_RECEIVED, 2, 7, "A").pack()
        self.assertEqual(len(conf), 28)
        full = P.Message(2, 1, 5, P.MSG_TEXT, 0, 7, "A", "x" * 80).pack()
        self.assertEqual(len(full), 91)
        self.assertEqual(P.unpack_message(full).text, "x" * 63)


class Mesh(unittest.TestCase):
    def test_seen_window(self):
        seen = SeenTable()
        self.assertFalse(seen.already_seen(1, 100, 0))
        self.assertTrue(seen.already_seen(1, 100, 1))
        self.assertTrue(seen.already_seen(1, 85, 1))      # one of the 15 before
        self.assertFalse(seen.already_seen(1, 101, 1))
        self.assertFalse(seen.already_seen(1, 101, 70))   # older than a minute


class Chat(unittest.TestCase):
    def test_message_to_one_node(self):
        a, b = C.Chat(SeenTable()), C.Chat(SeenTable())
        a.send(2, "B", "Ciao", 0)
        pkt = a.next_packet(1, "A", 0)
        self.assertEqual(P.unpack_message(pkt).text, "Ciao")
        self.assertEqual(b.receive(2, "B", pkt, 0), C.EVENT_NEW)
        self.assertIsNone(b.next_packet(2, "B", 0.1))
        conf = b.next_packet(2, "B", 1.0)
        self.assertEqual(P.unpack_message(conf).kind, P.MSG_RECEIVED)
        self.assertEqual(a.receive(1, "A", conf, 1.0), C.EVENT_STATUS)
        self.assertEqual(a.messages[0].status, C.DELIVERED)
        b.mark_conversation_read(2, "B", 1, False, 2.0)
        read = b.next_packet(2, "B", 2.2)
        a.receive(1, "A", read, 2.2)
        self.assertEqual(a.messages[0].status, C.READ)

    def test_no_answer_after_four_tries(self):
        a = C.Chat(SeenTable())
        a.send(2, "B", "x", 0)
        sent = sum(1 for t in range(0, 60) if a.next_packet(1, "A", float(t)))
        self.assertEqual(sent, 4)
        self.assertEqual(a.messages[0].status, C.NO_ANSWER)

    def test_relay_of_messages_for_others(self):
        c = C.Chat(SeenTable())
        pkt = P.Message(2, 1, 9, P.MSG_TEXT, 0, 3, "A", "hi").pack()
        c.receive(3, "C", pkt, 0)
        relayed = c.next_packet(3, "C", 2.0)
        self.assertEqual(relayed, P.with_ttl(pkt, 1))


class Alarm(unittest.TestCase):
    def test_head_on(self):
        own, tr = Own(), Traffic()
        own.update(45.0, 12.0, 5.0, 0.0, 0)
        # 0.5 NM north, coming south at 5 knots
        tr.heard(P.Position(2, 7, 0, 0, 45.0 + 926 / 110540.0, 12.0, 180.0, 5.0, "B"), -80, 0)
        tr.update(own, AlarmSettings(), 0)
        t = tr.targets[7]
        self.assertTrue(t.cpa_valid)
        self.assertAlmostEqual(t.tcpa_s, 180, delta=2)
        self.assertTrue(t.alarm)
        tr.set_quiet(7, True)
        tr.update(own, AlarmSettings(), 0)
        self.assertFalse(t.alarm)


class Ais(unittest.TestCase):
    def test_decodes(self):
        try:
            from pyais import decode
        except ImportError:
            self.skipTest("pyais not installed")
        m = A.mmsi_for(0xA7C94F1B, {}, 999)
        self.assertTrue(999000000 <= m < 1000000000)
        msg = decode(A.position_report(m, 45.46366, 12.14712, 4.2, 161.3))
        self.assertEqual((msg.msg_type, msg.mmsi), (18, m))
        self.assertAlmostEqual(msg.lat, 45.46366, places=4)
        self.assertAlmostEqual(msg.lon, 12.14712, places=4)
        self.assertAlmostEqual(msg.speed, 4.2)
        self.assertAlmostEqual(msg.course, 161.3)
        a, b = A.static_report(m, "Gejsha", 37)
        self.assertEqual(decode(a).shipname, "GEJSHA")
        self.assertEqual(decode(b).ship_type, 37)


class Gps(unittest.TestCase):
    def test_nmea(self):
        from navplotter.gps import Nmea
        n = Nmea()
        self.assertIsNone(n.feed("$GPGGA,092750.000,5321.6802,N,00630.3372,W,1,8,1.03,61.7,M,55.2,M,,*76"))
        fix = n.feed("$GPRMC,092750.000,A,5321.6802,N,00630.3372,W,0.02,31.66,280511,,,A*43")
        self.assertAlmostEqual(fix[0], 53.36134, places=5)
        self.assertAlmostEqual(fix[1], -6.50562, places=5)
        self.assertEqual(fix[2:], (0.02, 31.66, "09:27:50", 8, 1.03))
        self.assertIsNone(n.feed("$GPRMC,092750.000,A,5321.6802,N,00630.3372,W,0.02,31.66,280511,,,A*44"))  # bad checksum
        self.assertIsNone(n.feed("$GNRMC,,V,,,,,,,,,,N*4D"))                                                  # no fix yet

    def test_gps_of_the_v4_modem(self):
        import queue
        from navplotter.gps import GpsSource
        from navplotter.modem import Modem
        fixes = queue.Queue()
        gps = GpsSource({"source": "serial", "serial_port": "auto"}, lambda *f: fixes.put(f))

        class Port:             # what the modem prints, then the cable is pulled
            lines = [b"GPS $GNRMC,,V,,,,,,,,,,N*4D\n",
                     b"GPS $GPRMC,092750.000,A,5321.6802,N,00630.3372,W,0.02,31.66,280511,,,A*43\n", b"TXOK\n"]

            def readline(self):
                if not self.lines:
                    raise OSError("gone")
                return self.lines.pop(0)

        done = []
        modem = Modem("/nonexistent", None, lambda ok, why: done.append(ok), on_gps=gps.feed_modem)
        modem.port = Port()
        with self.assertRaises(OSError):
            modem._read_loop()
        self.assertEqual(done, [True])
        fix = fixes.get(timeout=10)     # the serial source finds no USB GPS and takes the modem's
        self.assertAlmostEqual(fix[0], 53.36134, places=5)
        self.assertEqual((fix[-1], gps.status, gps.baud), ("modem", "GPS modem", 9600))


class Camera(unittest.TestCase):
    def test_hailo_nms_result(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        from navplotter.camera import HailoDetector, Labels
        d = HailoDetector.__new__(HailoDetector)
        d.np, d.w, d.h, d.labels = np, 640, 640, Labels()
        # 1280x720 frame letterboxed into 640x640: scale 0.5, 140 pixels of padding above and below
        k, px, py = 0.5, 0, 140
        boat = [(140 + 90) / 640, 160 / 640, (140 + 270) / 640, 480 / 640, 0.9]   # y0 x0 y1 x1 score, of the input
        flat = [0.0] * 8 + [2.0] + boat + [0.2, 0.2, 0.3, 0.3, 0.1] + [0.0] * 71    # class 8 = boat, the second too weak
        for result in (np.array(flat, dtype=np.float32), [np.zeros((0, 5))] * 8 + [np.array([boat, [.2, .2, .3, .3, .1]])]):
            found = d._parse(result, 1280, 720, k, px, py, 0.4)
            self.assertEqual(len(found), 1)
            label, score, box = found[0]
            self.assertEqual(label, "boat")
            self.assertAlmostEqual(score, 0.9, places=5)
            for got, want in zip(box, (0.25, 0.25, 0.75, 0.75)):
                self.assertAlmostEqual(got, want, places=4)

    def test_own_model_and_its_classes(self):
        from navplotter.camera import Labels, find_hef
        tmp = tempfile.mkdtemp()
        self.assertEqual(Labels.for_model(None).names[8], "boat")          # COCO without a model of our own
        hef = os.path.join(tmp, "navplotter-mare.hef")
        open(hef, "wb").close()
        with open(os.path.join(tmp, "navplotter-mare.json"), "w") as f:
            json.dump({"names": ["boat", "ship", "person", "buoy"], "it": {"buoy": "boa"}, "important": ["person"]}, f)
        self.assertEqual(find_hef("auto", tmp), hef)
        lab = Labels.for_model(hef)
        self.assertEqual((lab.names[3], lab.name("buoy"), lab.important), ("buoy", "boa", {"person"}))

    def test_recordings(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("opencv not installed")
        from types import SimpleNamespace
        from http.server import ThreadingHTTPServer
        import io
        import zipfile
        from navplotter import camera
        tmp = tempfile.mkdtemp()
        cam = camera.Camera(SimpleNamespace(data=tmp, device="auto", hef="auto"))
        cam.start_recording()
        frame = np.zeros((48, 64, 3), np.uint8)
        for _ in range(3):
            cam.rec_last = 0
            cam._record(cv2, frame)
        name = cam.recording
        self.assertEqual(cam.status()["recording"], {"name": name, "frames": 3})
        cam.stop_recording()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), camera.make_handler(cam))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            recs = json.load(urllib.request.urlopen(base + "/recordings"))["recordings"]
            self.assertEqual((recs[0]["name"], recs[0]["frames"]), (name, 3))
            z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(base + "/recordings/%s.zip" % name).read()))
            self.assertEqual(len(z.namelist()), 3)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/recordings/..%2F..%2Fetc.zip")
            urllib.request.urlopen(urllib.request.Request(base + "/recordings/delete", data=("name=" + name).encode()))
            self.assertEqual(json.load(urllib.request.urlopen(base + "/recordings"))["recordings"], [])
        finally:
            srv.shutdown()

    def test_proxy_without_camera_service(self):
        from navplotter import web
        tmp = tempfile.mkdtemp()
        node, clock = make_node(tmp)
        node.s["camera_port"] = 9          # nothing listens there
        server = web.start(node, os.path.join(ROOT, "web"), 0)
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen("http://127.0.0.1:%d/camera/status" % server.server_address[1])
            self.assertEqual(e.exception.code, 503)
        finally:
            server.shutdown()


class NodeTraffic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.node, self.clock = make_node(self.tmp)

    def test_relays_and_ignores_copies(self):
        pkt = P.Position(2, 0xA7C94F1B, 5, 0, 45.46, 12.14, 0, 0, "Gejsha").pack()
        self.node.on_packet(pkt, -60)
        self.assertIn(0xA7C94F1B, self.node.traffic.targets)
        self.clock.t += 2
        self.node.tick()
        self.assertEqual(self.node.modem.sent, [P.with_ttl(pkt, 1)])
        self.node.on_tx_done(True, "")
        self.node.on_packet(pkt, -60)            # the same packet again: not relayed twice
        self.clock.t += 2
        self.node.tick()
        self.assertEqual(len(self.node.modem.sent), 1)

    def test_own_position_and_own_echo(self):
        self.clock.t += 9                        # the first position goes out 5-8 s after start
        self.node.on_fix(45.4637, 12.1471, 0.0, None, "11:54:00", 8, 0.9, "gpsd")
        self.node.tick()
        self.assertEqual(len(self.node.modem.sent), 1)
        pos = P.unpack_position(self.node.modem.sent[0])
        self.assertEqual((pos.id, pos.ttl, pos.name), (0xB8DF802C, 2, "RPI-802C"))
        # Our own packet relayed back by another node is not a target
        self.node.on_packet(P.with_ttl(self.node.modem.sent[0], 1), -50)
        self.assertEqual(self.node.traffic.targets, {})

    def test_no_position_without_share(self):
        self.node.s["share"] = False
        self.clock.t += 9
        self.node.on_fix(45.4637, 12.1471, 0.0, None, None, 8, 0.9, "gpsd")
        self.node.tick()
        self.assertEqual(self.node.modem.sent, [])


class Web(unittest.TestCase):
    def test_api(self):
        from navplotter import web
        tmp = tempfile.mkdtemp()
        node, clock = make_node(tmp)
        node.on_packet(P.Position(2, 0xA7C94F1B, 5, 0, 45.46, 12.14, 0, 0, "Gejsha").pack(), -60)
        server = web.start(node, os.path.join(ROOT, "web"), 0)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            state = json.load(urllib.request.urlopen(base + "/api/state"))
            self.assertEqual(state["targets"][0]["name"], "Gejsha")
            self.assertIn("msgStamp", state)
            self.assertEqual(state["gpsBaud"], 0)               # no GPS: the web app says "GPS not found"
            req = urllib.request.Request(base + "/api/config", data=b"name=Barca%20Uno&alarmRange=6&type=9", method="POST")
            cfg = json.load(urllib.request.urlopen(req))
            self.assertEqual((cfg["name"], cfg["alarmRange"], cfg["type"]), ("Barca Uno", 6, 0))
            with open(os.path.join(tmp, "settings.json")) as f:
                self.assertEqual(json.load(f)["name"], "Barca Uno")
            req = urllib.request.Request(base + "/api/messages", data=b"to=A7C94F1B&text=ciao", method="POST")
            self.assertTrue(json.load(urllib.request.urlopen(req))["ok"])
            msgs = json.load(urllib.request.urlopen(base + "/api/messages"))
            self.assertEqual(msgs["messages"][0]["name"], "Gejsha")
            r = urllib.request.urlopen(base + "/")
            self.assertEqual(r.headers["Content-Encoding"], "gzip")
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), None)  # the page itself needs no CORS
            r = urllib.request.urlopen(base + "/api/state")
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
