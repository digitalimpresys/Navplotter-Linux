"""Other nodes heard over LoRa, and the collision alarm (CPA/TCPA), as in the Heltec node (docs/PROTOCOL.md, section 5)."""
import math
from dataclasses import dataclass, field

from . import protocol as P

KN = 0.514444               # knots -> m/s
MOVING_MIN_KNOTS = 0.5
STALE_S, EXPIRE_S = 60.0, 180.0
MAX_TARGETS = 30
QUIET_MAX = 16
ALARM_PROFILE_CUSTOM = 4
ALARM_PROFILES = [(0.05, 2), (0.1, 5), (0.5, 10), (1.0, 20)]   # (passing distance NM, warning time min)


def stale_s(sleep_min):
    return sleep_min * 90.0 + 30.0 if sleep_min else STALE_S


def expire_s(sleep_min):
    return sleep_min * 180.0 + 60.0 if sleep_min else EXPIRE_S


@dataclass
class Target:
    id: int
    name: str = ""
    type: int = P.NODE_BOAT
    simulated: bool = False
    lat: float = 0.0
    lon: float = 0.0
    cog: float = 0.0
    sog: float = 0.0
    last_seen: float = 0.0
    stale_after: float = STALE_S
    expire_after: float = EXPIRE_S
    rssi: float = 0.0
    relayed: bool = False
    cpa_valid: bool = False
    alarm: bool = False
    cpa_nm: float = 0.0
    tcpa_s: float = 0.0
    dist_nm: float = 0.0
    brg: float = 0.0
    updates: int = 0            # how many packets so far: tells the AIS output something is new

    def stale(self, now):
        return now - self.last_seen > self.stale_after


@dataclass
class Own:
    """Our own position and motion, from gpsd, Signal K or the settings."""
    lat: float = 0.0
    lon: float = 0.0
    sog: float = 0.0            # knots
    course: float = 0.0         # degrees; the last good course is kept while stopped
    course_known: bool = False
    fix_time: float = -1e9      # monotonic time of the last fix
    sats: int = 0
    hdop: float = 99.9
    utc: str = "00:00:00"
    source: str = ""

    def fix(self, now):
        return now - self.fix_time < 5.0

    def moving(self):
        return self.sog >= MOVING_MIN_KNOTS

    def update(self, lat, lon, sog, cog, now, utc=None, sats=None, hdop=None, source=""):
        self.lat, self.lon, self.sog = lat, lon, sog or 0.0
        # When stopped the GPS course is noise, so the last good one is kept
        if cog is not None and self.moving():
            self.course, self.course_known = cog % 360.0, True
        self.fix_time = now
        if utc:
            self.utc = utc
        if sats is not None:
            self.sats = sats
        if hdop is not None:
            self.hdop = hdop
        self.source = source


@dataclass
class AlarmSettings:
    range_nm: int = 3           # 3, 6, 12; 0 = no limit
    profile: int = 1
    cpa_nm: float = 0.1         # the custom profile
    tcpa_min: int = 5

    def limits(self):
        if self.profile < ALARM_PROFILE_CUSTOM:
            cpa, tcpa = ALARM_PROFILES[self.profile]
        else:
            cpa, tcpa = self.cpa_nm, self.tcpa_min
        return cpa, tcpa * 60.0


@dataclass
class Traffic:
    targets: dict = field(default_factory=dict)
    quiet: list = field(default_factory=list)
    alarm_name: str = ""
    alarm_active: bool = False
    rx_count: int = 0

    def heard(self, pos, rssi, now):
        """A new position (not a copy already seen)."""
        self.rx_count += 1
        t = self.targets.get(pos.id)
        if t is None:
            if len(self.targets) >= MAX_TARGETS:
                return None
            t = self.targets[pos.id] = Target(pos.id)
        t.name, t.type, t.simulated = pos.name, pos.node_type, bool(pos.flags & P.FLAG_SIMULATED)
        t.lat, t.lon, t.cog, t.sog = pos.lat, pos.lon, pos.cog, pos.sog
        t.last_seen = now
        t.stale_after, t.expire_after = stale_s(pos.sleep_minutes), expire_s(pos.sleep_minutes)
        t.rssi, t.relayed = rssi, pos.ttl < P.MESH_TTL
        t.updates += 1
        return t

    def set_quiet(self, nid, on):
        if nid == 0:
            return
        if on and nid not in self.quiet:
            if len(self.quiet) >= QUIET_MAX:
                self.quiet.pop(0)
            self.quiet.append(nid)
        elif not on and nid in self.quiet:
            self.quiet.remove(nid)

    def update(self, own, alarm, now):
        """Once a second: drop lost targets and recompute approaches."""
        for nid in [k for k, t in self.targets.items() if now - t.last_seen > t.expire_after]:
            del self.targets[nid]
        worst = None
        for t in self.targets.values():
            self._update_target(t, own, alarm, now)
            if t.alarm and (worst is None or t.tcpa_s < worst.tcpa_s):
                worst = t
        self.alarm_active = worst is not None
        self.alarm_name = worst.name if worst else ""

    def _update_target(self, t, own, alarm, now):
        t.cpa_valid = t.alarm = False
        if not own.fix(now):
            return
        stale = t.stale(now)
        age = now - t.last_seen
        tgt_sog, tgt_cog = t.sog * KN, math.radians(t.cog)
        # Relative position on a local flat plane in metres; a fresh target is moved forward by its age
        rx = (t.lon - own.lon) * 111320.0 * math.cos(math.radians(own.lat)) + (0 if stale else tgt_sog * math.sin(tgt_cog) * age)
        ry = (t.lat - own.lat) * 110540.0 + (0 if stale else tgt_sog * math.cos(tgt_cog) * age)
        t.dist_nm = math.hypot(rx, ry) / 1852.0
        t.brg = math.degrees(math.atan2(rx, ry)) % 360.0
        if stale:
            return
        own_sog = own.sog * KN if own.moving() else 0.0
        own_cog = math.radians(own.course)
        vx = tgt_sog * math.sin(tgt_cog) - own_sog * math.sin(own_cog)
        vy = tgt_sog * math.cos(tgt_cog) - own_sog * math.cos(own_cog)
        v2 = vx * vx + vy * vy
        if v2 < 0.06:           # relative speed under half a knot: nothing is closing in
            return
        tcpa = -(rx * vx + ry * vy) / v2
        if tcpa < 0:            # moving apart
            return
        t.cpa_valid, t.tcpa_s = True, tcpa
        t.cpa_nm = math.hypot(rx + vx * tcpa, ry + vy * tcpa) / 1852.0
        cpa_lim, tcpa_lim = alarm.limits()
        t.alarm = ((alarm.range_nm == 0 or t.dist_nm <= alarm.range_nm) and t.cpa_nm <= cpa_lim and t.tcpa_s <= tcpa_lim
                   and t.id not in self.quiet)
