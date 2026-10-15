"""Our own position: from Signal K, from gpsd, or a fixed one from the settings.

"auto" tries Signal K first (the boat GPS, already on the boat network) and then gpsd.
"""
import json
import logging
import math
import socket
import threading
import time
import urllib.request

log = logging.getLogger("navplotter.gps")
MS_TO_KN = 1.0 / 0.514444


class GpsSource:
    """Runs in its own thread and calls on_fix(lat, lon, sog_kn, cog_deg_or_None, utc, sats, hdop, source)."""

    def __init__(self, settings, on_fix):
        self.s = settings
        self.on_fix = on_fix
        self.status = "starting"
        threading.Thread(target=self._run, name="gps", daemon=True).start()

    def _run(self):
        kind = self.s.get("source", "auto")
        while True:
            try:
                if kind == "fixed":
                    self._fixed()
                elif kind == "signalk":
                    self._signalk()
                elif kind == "gpsd":
                    self._gpsd()
                else:
                    try:
                        self._signalk()
                    except Exception as e:
                        log.info("Signal K not usable (%s), trying gpsd", e)
                        self.status = "Signal K: %s" % e
                        self._gpsd()
            except Exception as e:
                self.status = "%s: %s" % (kind, e)
                log.warning("position source %s: %s", kind, e)
            time.sleep(5)

    def _fixed(self):
        self.status = "fixed"
        while True:
            self.on_fix(float(self.s["lat"]), float(self.s["lon"]), 0.0, None, time.strftime("%H:%M:%S", time.gmtime()), 0, 99.9, "fixed")
            time.sleep(1)

    def _signalk(self):
        url = self.s.get("signalk_url", "http://127.0.0.1:3000").rstrip("/") + "/signalk/v1/api/vessels/self/navigation"
        headers = {"Accept": "application/json"}
        if self.s.get("signalk_token"):
            headers["Authorization"] = "Bearer " + self.s["signalk_token"]
        last_ts = None
        while True:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3) as r:
                nav = json.load(r)
            pos = nav.get("position", {})
            value = pos.get("value", {})
            ts = pos.get("timestamp")
            if "latitude" not in value:
                raise RuntimeError("no position in Signal K")
            self.status = "Signal K"
            # A position that does not change its timestamp is old: no fix (the boat GPS went quiet)
            if ts != last_ts:
                last_ts = ts
                sog = nav.get("speedOverGround", {}).get("value")
                cog = nav.get("courseOverGroundTrue", {}).get("value")
                utc = ts[11:19] if ts and len(ts) >= 19 else None
                self.on_fix(value["latitude"], value["longitude"], (sog or 0.0) * MS_TO_KN,
                            math.degrees(cog) if cog is not None else None, utc, None, None, "signalk")
            time.sleep(1)

    def _gpsd(self):
        host = self.s.get("gpsd_host", "127.0.0.1")
        port = int(self.s.get("gpsd_port", 2947))
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            self.status = "gpsd"
            buf = b""
            sats, hdop = 0, 99.9
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    raise RuntimeError("gpsd closed the connection")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("class") == "SKY":
                        sats = sum(1 for s in msg.get("satellites", []) if s.get("used")) or msg.get("uSat", sats)
                        hdop = msg.get("hdop", hdop)
                    elif msg.get("class") == "TPV" and msg.get("mode", 0) >= 2 and "lat" in msg:
                        t = msg.get("time", "")
                        self.on_fix(msg["lat"], msg["lon"], msg.get("speed", 0.0) * MS_TO_KN, msg.get("track"),
                                    t[11:19] if len(t) >= 19 else None, sats, hdop, "gpsd")
