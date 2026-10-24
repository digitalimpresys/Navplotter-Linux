"""Tests of the pan/tilt of a network camera (navplotter/onvif.py) against a small fake ONVIF camera."""
import base64
import hashlib
import os
import sys
import threading
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from navplotter.onvif import Ptz, OnvifError, _find, _local  # noqa: E402

USER, PASSWORD = "nav", "p&ss<w>"

TIME = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><tds:GetSystemDateAndTimeResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:SystemDateAndTime>
<tt:UTCDateTime><tt:Time><tt:Hour>10</tt:Hour><tt:Minute>0</tt:Minute><tt:Second>0</tt:Second></tt:Time>
<tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>24</tt:Day></tt:Date></tt:UTCDateTime>
</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse></s:Body></s:Envelope>"""
CAPS = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><tds:GetCapabilitiesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:Capabilities>
<tt:Media><tt:XAddr>http://10.9.9.9:PORT/onvif/service</tt:XAddr></tt:Media>%s
</tds:Capabilities></tds:GetCapabilitiesResponse></s:Body></s:Envelope>"""
PTZ_CAP = "<tt:PTZ><tt:XAddr>http://10.9.9.9:PORT/onvif/service</tt:XAddr></tt:PTZ>"
PROFILES = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
<trt:GetProfilesResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl"><trt:Profiles token="profile_1" fixed="true">
</trt:Profiles><trt:Profiles token="profile_2"></trt:Profiles></trt:GetProfilesResponse></s:Body></s:Envelope>"""
OK = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>"""
FAULT = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:ter="http://www.onvif.org/ver10/error">
<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>%s</s:Value></s:Subcode></s:Code>
<s:Reason><s:Text xml:lang="en">%s</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>"""


class FakeCamera:
    def __init__(self, ptz=True, continuous=True, port=0):
        self.calls = []
        cam = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                root = ET.fromstring(self.rfile.read(int(self.headers["Content-Length"])))
                body = _find(root, "Body")[0]
                op = _local(body.tag)
                cam.calls.append((self.path, op, body))
                if op == "GetSystemDateAndTime":
                    return self.answer(200, TIME)
                if not cam.authorised(root):
                    return self.answer(400, FAULT % ("ter:NotAuthorized", "Sender not Authorized"))
                if op == "GetCapabilities":
                    return self.answer(200, (CAPS % (PTZ_CAP if ptz else "")).replace("PORT", str(cam.port)))
                if op == "GetProfiles":
                    return self.answer(200, PROFILES)
                if op == "ContinuousMove" and not continuous:
                    return self.answer(500, FAULT % ("ter:ActionNotSupported", "Optional Action Not Implemented"))
                return self.answer(200, OK)

            def answer(self, code, text):
                data = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def authorised(self, root):
        token = _find(root, "UsernameToken")
        if token is None or _find(token, "Username").text != USER:
            return False
        nonce = base64.b64decode(_find(token, "Nonce").text)
        created = _find(token, "Created").text
        want = base64.b64encode(hashlib.sha1(nonce + created.encode() + PASSWORD.encode()).digest()).decode()
        return _find(token, "Password").text == want and created.startswith("2026-10-24T10:0")

    def ops(self):
        return [op for _, op, _ in self.calls]


class TestPtz(unittest.TestCase):
    def tearDown(self):
        self.cam.server.shutdown()

    def test_move_and_stop(self):
        self.cam = FakeCamera()
        p = Ptz("127.0.0.1", self.cam.port, USER, PASSWORD).connect()
        self.assertEqual(p.profile, "profile_1")
        # The address the camera gives is not reachable: the known host and port instead
        self.assertEqual(p.ptz_url, "http://127.0.0.1:%d/onvif/service" % self.cam.port)
        p.move(0.5, 0)
        p.stop()
        self.assertEqual(self.cam.ops(), ["GetSystemDateAndTime", "GetCapabilities", "GetProfiles", "ContinuousMove", "Stop"])
        move = self.cam.calls[3][2]
        pan = _find(move, "PanTilt")
        self.assertEqual((pan.get("x"), pan.get("y")), ("0.50", "0.00"))
        self.assertEqual(_find(move, "ProfileToken").text, "profile_1")

    def test_steps_when_continuous_is_refused(self):
        self.cam = FakeCamera(continuous=False)
        p = Ptz("127.0.0.1", self.cam.port, USER, PASSWORD).connect()
        p.move(0, -0.5)
        p.stop()            # nothing to stop after a step
        p.move(0, 0.5)
        self.assertEqual(self.cam.ops()[3:], ["ContinuousMove", "RelativeMove", "RelativeMove"])
        pan = _find(self.cam.calls[-1][2], "PanTilt")
        self.assertEqual((pan.get("x"), pan.get("y")), ("0.00", "0.05"))

    def test_wrong_password(self):
        self.cam = FakeCamera()
        with self.assertRaisesRegex(OnvifError, "password"):
            Ptz("127.0.0.1", self.cam.port, USER, "nope").connect()

    def test_no_pan_tilt(self):
        self.cam = FakeCamera(ptz=False)
        with self.assertRaisesRegex(OnvifError, "no pan and tilt"):
            Ptz("127.0.0.1", self.cam.port, USER, PASSWORD).connect()

    def test_unreachable(self):
        self.cam = FakeCamera()
        with self.assertRaisesRegex(OnvifError, "no ONVIF"):
            Ptz("127.0.0.1", 1, USER, PASSWORD, timeout=1).connect()


if __name__ == "__main__":
    unittest.main()
