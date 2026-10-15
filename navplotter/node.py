"""The NavPlotter node: radio traffic, mesh, chat, alarm and AIS output, with the same rules as the Heltec node."""
import calendar
import logging
import os
import random
import threading
import time

from . import ais as A
from . import protocol as P
from .chat import Chat, EVENT_NEW
from .mesh import RelayQueue, SeenTable
from .track import Track
from .traffic import AlarmSettings, Own, Traffic

log = logging.getLogger("navplotter.node")
VERSION = "0.1.0"

TX_MOVING_S, TX_STOPPED_S = 10.0, 30.0
TX_TIMEOUT_S = 3.0


class Node:
    def __init__(self, settings, clock=time.monotonic):
        self.s = settings
        self.clock = clock
        self.lock = threading.RLock()
        self.own = Own()
        self.traffic = Traffic()
        self.seen = SeenTable()
        self.pos_relays = RelayQueue(4)
        self.chat = Chat(self.seen)
        self.modem = None
        self.my_id = int(settings["id"], 16) if settings["id"] else None
        self.seq = 0
        self.transmitting = False
        self.tx_is_relay = False
        self.tx_start = 0.0
        self.tx_count = self.relay_count = self.tx_errors = 0
        now = clock()
        self.started = now
        self.next_tx = now + random.uniform(5, 8)
        self.last_second = now
        self.ais_next = now
        self.ais_static_next = now
        self.ais_new = set()
        os.makedirs(settings["data_dir"], exist_ok=True)
        self.track = Track(os.path.join(settings["data_dir"], "track.json"))
        self.ais = A.AisOutput(settings["ais"]["destinations"]) if settings["ais"]["enabled"] else None

    # ---- Settings ----

    @property
    def name(self):
        n = self.s["name"]
        if not n and self.my_id is not None:
            n = "RPI-%04X" % (self.my_id & 0xFFFF)
        return n or "RPI"

    def alarm(self):
        return AlarmSettings(self.s["alarmRange"], self.s["alarmProfile"], self.s["alarmCpa"], self.s["alarmTcpa"])

    # ---- From the modem and the GPS (other threads) ----

    def on_modem_ready(self, modem_id):
        with self.lock:
            if self.my_id is None and modem_id is not None:
                self.my_id = modem_id
                log.info("node id %08X (from the modem), name %s", self.my_id, self.name)

    def on_tx_done(self, ok, reason):
        with self.lock:
            if not self.transmitting:
                return
            self.transmitting = False
            if ok:
                if self.tx_is_relay:
                    self.relay_count += 1
                else:
                    self.tx_count += 1
            else:
                self.tx_errors += 1

    def on_fix(self, lat, lon, sog, cog, utc, sats, hdop, source):
        with self.lock:
            now = self.clock()
            self.own.update(lat, lon, sog, cog, now, utc, sats, hdop, source)

    def on_packet(self, data, rssi, snr=0.0):
        with self.lock:
            now = self.clock()
            if len(data) == P.POS_LEN and data[1:2] == b"P":
                pos = P.unpack_position(data)
                if pos is None or pos.id == self.my_id:
                    return
                # Mesh: drop copies already received (and if somebody else relayed it, we do not need to)
                if self.seen.already_seen(pos.id, pos.seq, now):
                    self.pos_relays.cancel(pos.id, pos.seq)
                    return
                if pos.ttl > 0:
                    self.pos_relays.add(pos.id, pos.seq, P.with_ttl(data, pos.ttl - 1), now)
                t = self.traffic.heard(pos, rssi, now)
                if t is not None and t.updates == 1:
                    self.ais_new.add(t.id)
                log.debug("RX %s%s %.5f %.5f %.1f kn %.0f deg RSSI %.0f", pos.name, " (relayed)" if pos.ttl < P.MESH_TTL else "",
                          pos.lat, pos.lon, pos.sog, pos.cog, rssi)
            elif data[:2] == b"NM":
                if self.my_id is None:
                    return
                if self.chat.receive(self.my_id, self.name, data, now) & EVENT_NEW:
                    m = self.chat.messages[-1]
                    log.info("message from %s%s: %s", m.peer_name, " (to all)" if m.to_all else "", m.text)

    # ---- The main loop ----

    def tick(self):
        with self.lock:
            now = self.clock()
            self._radio(now)
            if now - self.last_second >= 1.0:
                self.last_second = now
                self.traffic.update(self.own, self.alarm(), now)
                self._track(now)
            self._ais(now)

    def _transmit(self, data, relay, now):
        if self.modem is None or not self.modem.transmit(data):
            return False
        self.transmitting, self.tx_is_relay, self.tx_start = True, relay, now
        return True

    def _radio(self, now):
        if self.transmitting and now - self.tx_start > TX_TIMEOUT_S:
            self.transmitting = False
            self.tx_errors += 1
        if self.transmitting or self.my_id is None:
            return
        # Our own position goes out only if sharing is on; receiving and relaying the others do not depend on it
        if now >= self.next_tx:
            if self.s["share"] and self.own.fix(now) and self._send_position(now):
                self.next_tx = now + (TX_MOVING_S if self.own.moving() else TX_STOPPED_S) + random.uniform(0, 2)
                return
            self.next_tx = now + 2
        pkt = self.chat.next_packet(self.my_id, self.name, now)
        if pkt is not None and self._transmit(pkt, True, now):
            return
        pkt = self.pos_relays.next_due(now)
        if pkt is not None:
            self._transmit(pkt, True, now)

    def _send_position(self, now):
        flags = (self.s["type"] & P.TYPE_MASK) << P.TYPE_SHIFT
        pos = P.Position(P.MESH_TTL, self.my_id, self.seq, flags, self.own.lat, self.own.lon, self.own.course,
                         self.own.sog if self.own.moving() else 0.0, self.name)
        if not self._transmit(pos.pack(), False, now):
            return False
        self.seq = (self.seq + 1) & 0xFF
        return True

    def _track(self, now):
        # Signal K gives no HDOP: its position is taken as good
        if self.own.fix(now) and (self.own.hdop < 5.0 or self.own.source == "signalk"):
            self.track.add(self.own.lat, self.own.lon, self._utc_epoch())
        try:
            self.track.save()
        except OSError as e:
            log.warning("track not saved: %s", e)

    def _utc_epoch(self):
        if self.own.utc and self.own.utc != "00:00:00":
            day = time.strftime("%Y-%m-%d ", time.gmtime())
            try:
                return calendar.timegm(time.strptime(day + self.own.utc, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
        return int(time.time())

    # ---- AIS output ----

    def _ais(self, now):
        if self.ais is None:
            return
        cfg = self.s["ais"]
        static = now >= self.ais_static_next
        if now < self.ais_next and not self.ais_new and not static:
            return
        if now >= self.ais_next:
            self.ais_next = now + cfg["every_s"]
            ids = list(self.traffic.targets)
        else:
            ids = list(self.ais_new)
        if static:
            self.ais_static_next = now + cfg["static_every_s"]
        out = []
        for nid in ids:
            t = self.traffic.targets.get(nid)
            if t is None or t.stale(now) or (t.simulated and not cfg["include_simulated"]):
                continue
            mmsi = A.mmsi_for(nid, cfg["mmsi"], cfg["mmsi_prefix"])
            out.append(A.position_report(mmsi, t.lat, t.lon, t.sog, t.cog))
            if static or nid in self.ais_new:
                out.extend(A.static_report(mmsi, t.name, A.SHIP_TYPE.get(t.type, 0)))
        self.ais_new.clear()
        if out:
            self.ais.send(out)

    # ---- For the web app ----

    def state_json(self):
        with self.lock:
            now = self.clock()
            o, fix = self.own, self.own.fix(now)
            targets = []
            for t in self.traffic.targets.values():
                targets.append({"id": "%08X" % t.id, "name": t.name, "type": t.type, "sim": t.simulated,
                                "lat": round(t.lat, 7), "lon": round(t.lon, 7), "sog": round(t.sog, 2), "cog": round(t.cog, 1),
                                "age": int(now - t.last_seen), "rssi": round(t.rssi), "relayed": t.relayed, "stale": t.stale(now),
                                "dist": round(t.dist_nm, 3), "brg": round(t.brg), "cpaValid": t.cpa_valid,
                                "cpa": round(t.cpa_nm, 3), "tcpa": round(t.tcpa_s), "alarm": t.alarm})
            ready = self.modem is not None and self.modem.ready
            return {
                "own": {"fix": fix, "lat": round(o.lat, 7) if fix else 0.0, "lon": round(o.lon, 7) if fix else 0.0,
                        "sog": round(o.sog, 2), "cog": round(o.course, 1), "cogValid": o.course_known, "moving": o.moving(),
                        "sats": o.sats, "hdop": o.hdop, "time": o.utc},
                "targets": targets,
                "alarm": self.traffic.alarm_active, "alarmName": self.traffic.alarm_name,
                "lora": {"ok": ready, "tx": self.tx_count, "rx": self.traffic.rx_count, "relay": self.relay_count, "freq": 869.525},
                "gpsBaud": 0, "sd": os.path.isdir(self.s["tiles_dir"]), "battery": 0, "uptime": int(now - self.started),
                "sta": True, "staIp": "", "apIp": "",
                "msgStamp": self.chat.stamp(), "quiet": ["%08X" % q for q in self.traffic.quiet],
                "track": self.track.state_json(),
            }

    def config_json(self, host):
        s = self.s
        return {"name": self.name, "type": s["type"], "share": s["share"], "alarmRange": s["alarmRange"], "lang": s["lang"],
                "tiles": s["tiles"], "sound": s["sound"], "id": "%08X" % (self.my_id or 0), "version": "Linux " + VERSION,
                "wifiOff": 0, "sleep": 0, "msg": True, "alarmProfile": s["alarmProfile"], "alarmCpa": round(s["alarmCpa"], 2),
                "alarmTcpa": s["alarmTcpa"], "apOnly": True, "net": 0, "staSsid": "", "staPassSet": False,
                "ssid": "", "host": host}

    def apply_config(self, form):
        """POST /api/config: the same fields and limits as the nodes; values out of range are ignored."""
        s = self.s
        with self.lock:
            if form.get("name", "").strip():
                s["name"] = P.clean_name(form["name"].strip().encode("ascii", "replace"))
            ints = {"type": (0, 2), "alarmProfile": (0, 4), "alarmTcpa": (1, 60), "tiles": (0, 2)}
            for k, (lo, hi) in ints.items():
                if k in form:
                    try:
                        v = int(form[k])
                        if lo <= v <= hi:
                            s[k] = v
                    except ValueError:
                        pass
            if form.get("alarmRange") in ("0", "3", "6", "12"):
                s["alarmRange"] = int(form["alarmRange"])
            if "alarmCpa" in form:
                try:
                    v = float(form["alarmCpa"])
                    if 0.02 <= v <= 5.0:
                        s["alarmCpa"] = v
                except ValueError:
                    pass
            for k in ("share", "sound"):
                if k in form:
                    s[k] = form[k] == "1"
            if "lang" in form:
                s["lang"] = "it" if form["lang"] == "it" else "en"
            s.save()

    def send_message(self, to, to_name, text):
        with self.lock:
            t = self.traffic.targets.get(to)
            self.chat.send(to, t.name if t else to_name, text, self.clock())

    def messages_read(self, peer, all_):
        with self.lock:
            if self.my_id is not None:
                self.chat.mark_conversation_read(self.my_id, self.name, peer, all_, self.clock())

    def messages_json(self):
        with self.lock:
            return self.chat.to_json(self.clock())
