"""The boat's track, as in nav_track.h: a point every 25 m made good, the web app reads it through /api/track."""
import json
import math
import os
import time

MAX_POINTS = 4000
STEP_M = 25.0
SAVE_S = 120.0


def metres(lat1, lon1, lat2, lon2):
    dy = (lat2 - lat1) * 110540.0
    dx = (lon2 - lon1) * 111320.0 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


class Track:
    def __init__(self, path):
        self.path = path
        self.points = []        # [lat, lon, utc seconds or 0]
        self.id = 1
        self.dropped = 0
        self.dirty = False
        self.last_save = time.monotonic()
        try:
            with open(path) as f:
                d = json.load(f)
            self.id, self.dropped, self.points = d["id"], d["dropped"], d["points"]
        except (OSError, ValueError, KeyError):
            pass

    def add(self, lat, lon, utc):
        if self.points and metres(self.points[-1][0], self.points[-1][1], lat, lon) < STEP_M:
            return
        if len(self.points) >= MAX_POINTS:
            drop = MAX_POINTS // 4
            del self.points[:drop]
            self.dropped += drop
        self.points.append([round(lat, 7), round(lon, 7), int(utc)])
        self.dirty = True

    def new(self):
        self.points, self.dropped = [], 0
        self.id += 1
        self.save(force=True)

    def save(self, force=False):
        if not (self.dirty or force) or (not force and time.monotonic() - self.last_save < SAVE_S):
            return
        self.last_save = time.monotonic()
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"id": self.id, "dropped": self.dropped, "points": self.points}, f)
        os.replace(tmp, self.path)
        self.dirty = False

    def length_nm(self):
        return sum(metres(a[0], a[1], b[0], b[1]) for a, b in zip(self.points, self.points[1:])) / 1852.0

    def state_json(self):
        return {"id": self.id, "n": self.dropped + len(self.points), "first": self.dropped}

    def points_json(self, start):
        start = max(start, self.dropped)
        pts = self.points[start - self.dropped:]
        # Degrees * 1e6, as the nodes send them
        return {"id": self.id, "first": self.dropped, "from": start,
                "points": [[round(p[0] * 1e6), round(p[1] * 1e6), p[2]] for p in pts]}

    def gpx(self, name):
        name = "".join(c for c in name if c not in "<>&")
        out = ['<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="NavPlotter" xmlns="http://www.topografix.com/GPX/1/1">\n'
               "<trk><name>%s</name><trkseg>\n" % name]
        for lat, lon, t in self.points:
            if t:
                out.append('<trkpt lat="%.6f" lon="%.6f"><time>%s</time></trkpt>\n' % (lat, lon, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))))
            else:
                out.append('<trkpt lat="%.6f" lon="%.6f"/>\n' % (lat, lon))
        out.append("</trkseg></trk>\n</gpx>\n")
        return "".join(out)
