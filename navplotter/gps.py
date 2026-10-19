"""Our own position: from a USB GPS, from Signal K, from gpsd, or a fixed one from the settings.

"serial" (the default): a GPS on a USB or serial port that sends NMEA (G-Mouse, u-blox and the like). With
port "auto" the first serial device that is not the modem is used; without one there is no position.
"none": no position of our own; the node still receives, relays and shows the others.
"auto" tries Signal K first (the boat GPS, already on the boat network) and then gpsd.
"""
import glob
import json
import logging
import os
import math
import socket
import threading
import time
import urllib.request

log = logging.getLogger("navplotter.gps")
MS_TO_KN = 1.0 / 0.514444


class GpsSource:
    """Runs in its own thread and calls on_fix(lat, lon, sog_kn, cog_deg_or_None, utc, sats, hdop, source)."""

    def __init__(self, settings, on_fix, modem_port=None):
        self.s = settings
        self.modem_port = modem_port
        # The web app shows "GPS not found" while this is 0: the port speed of a serial GPS, else the configured
        # speed as soon as another source gives data
        self.baud = 0

        def fix(*args):
            if not self.baud:
                self.baud = int(self.s.get("serial_baud", 9600))
            on_fix(*args)
        self.on_fix = fix
        self.status = "starting"
        threading.Thread(target=self._run, name="gps", daemon=True).start()

    def _run(self):
        kind = self.s.get("source", "auto")
        last_error = None
        if kind == "none":
            self.status = "none"
            return
        while True:
            try:
                if kind == "fixed":
                    self._fixed()
                elif kind == "serial":
                    self._serial()
                elif kind == "signalk":
                    self._signalk()
                elif kind == "gpsd":
                    self._gpsd()
                else:
                    try:
                        self._signalk()
                    except Exception as e:
                        self.status = "Signal K: %s" % e
                        try:
                            self._gpsd()
                        except Exception as e2:
                            raise RuntimeError("Signal K: %s; gpsd: %s" % (e, e2))
            except Exception as e:
                self.baud = 0
                self.status = "%s: %s" % (kind, e)
                # The same error every 5 s is logged once
                if self.status != last_error:
                    log.warning("no position (%s)", self.status)
                    last_error = self.status
            time.sleep(5)

    def _find_serial(self):
        port = self.s.get("serial_port", "auto")
        if port != "auto":
            return port
        modem = os.path.realpath(self.modem_port) if self.modem_port else None
        found = []
        for p in sorted(glob.glob("/dev/serial/by-id/*")):
            if "CP210" in p or os.path.realpath(p) == modem:
                continue        # the Heltec modem
            gps_like = any(k in p.lower() for k in ("u-blox", "gps", "gnss"))
            found.append((not gps_like, p))
        for p in sorted(glob.glob("/dev/ttyACM*")):
            if os.path.realpath(p) != modem and all(os.path.realpath(f) != p for _, f in found):
                found.append((True, p))
        if not found:
            raise RuntimeError("no USB GPS plugged in")
        return sorted(found)[0][1]

    def _serial(self):
        import serial
        name = self._find_serial()
        baud = int(self.s.get("serial_baud", 9600))
        with serial.Serial(name, baud, timeout=3) as port:
            log.info("GPS on %s", name)
            self.baud = baud
            self.status = "GPS " + name
            nmea = Nmea()
            silent = 0
            while True:
                line = port.readline()
                if not line:
                    silent += 1
                    if silent >= 5:
                        raise RuntimeError("no data from the GPS on %s" % name)
                    continue
                silent = 0
                fix = nmea.feed(line.decode("ascii", "replace").strip())
                if fix:
                    self.on_fix(*fix, "serial")

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


def _nmea_ok(line):
    """True if the line is an NMEA sentence with a right checksum (or none)."""
    if not line.startswith("$"):
        return False
    body, _, ck = line[1:].partition("*")
    if not ck:
        return True
    x = 0
    for c in body:
        x ^= ord(c)
    try:
        return x == int(ck[:2], 16)
    except ValueError:
        return False


def _deg(value, hemi):
    if not value:
        return None
    d = value.index(".") - 2
    deg = float(value[:d]) + float(value[d:]) / 60.0
    return -deg if hemi in ("S", "W") else deg


class Nmea:
    """Reads RMC (position, speed, course, time) and GGA (satellites, HDOP) from any talker (GP, GN, GL...)."""

    def __init__(self):
        self.sats, self.hdop = 0, 99.9

    def feed(self, line):
        """One line; returns (lat, lon, sog_kn, cog_or_None, utc, sats, hdop) for a valid RMC, else None."""
        if not _nmea_ok(line):
            return None
        f = line[1:].split("*")[0].split(",")
        kind = f[0][2:]
        try:
            if kind == "GGA" and len(f) > 8:
                if f[6] and f[6] != "0":
                    self.sats = int(f[7] or 0)
                    self.hdop = float(f[8] or 99.9)
            elif kind == "RMC" and len(f) > 8 and f[2] == "A":
                lat, lon = _deg(f[3], f[4]), _deg(f[5], f[6])
                if lat is None or lon is None:
                    return None
                t = f[1]
                utc = "%s:%s:%s" % (t[0:2], t[2:4], t[4:6]) if len(t) >= 6 else None
                return lat, lon, float(f[7] or 0.0), float(f[8]) if f[8] else None, utc, self.sats, self.hdop
        except ValueError:
            return None
        return None
