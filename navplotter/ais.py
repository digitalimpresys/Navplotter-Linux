"""NavPlotter nodes as AIS class B targets: NMEA 0183 !AIVDM sentences (types 18 and 24) for Signal K and OpenCPN.

Each node gets a made-up MMSI: settings "mmsi" can map a node id to a real one, otherwise it is
<prefix> followed by six digits from the node id (prefix 999 by default: no country uses it).
"""
import socket

SHIP_TYPE = {0: 37, 1: 0, 2: 0}         # boat = pleasure craft; person and object: not available


def mmsi_for(node_id, mapping, prefix):
    hexid = "%08X" % node_id
    if hexid in mapping:
        return int(mapping[hexid])
    return int(str(prefix)[:3].ljust(3, "9")) * 1_000_000 + node_id % 1_000_000


class Bits:
    def __init__(self):
        self.bits = []

    def uint(self, value, width):
        value &= (1 << width) - 1
        self.bits.extend((value >> (width - 1 - i)) & 1 for i in range(width))

    def int(self, value, width):
        self.uint(value if value >= 0 else (1 << width) + value, width)

    def text(self, s, chars):
        s = s.upper()[:chars].ljust(chars, "@")
        for c in s:
            o = ord(c)
            self.uint(o - 64 if 64 <= o < 96 else (o if 32 <= o < 64 else 0), 6)

    def payload(self):
        fill = (-len(self.bits)) % 6
        bits = self.bits + [0] * fill
        out = []
        for i in range(0, len(bits), 6):
            v = int("".join(map(str, bits[i:i + 6])), 2)
            v += 48
            out.append(chr(v + 8 if v > 87 else v))
        return "".join(out), fill


def sentence(payload, fill, channel="A"):
    body = "AIVDM,1,1,,%s,%s,%d" % (channel, payload, fill)
    cs = 0
    for c in body:
        cs ^= ord(c)
    return "!%s*%02X" % (body, cs)


def position_report(mmsi, lat, lon, sog, cog, second=60):
    """Type 18: class B position report."""
    b = Bits()
    b.uint(18, 6); b.uint(0, 2); b.uint(mmsi, 30); b.uint(0, 8)
    b.uint(min(round(sog * 10), 1022), 10)
    b.uint(0, 1)                                    # position accuracy: low (GPS)
    b.int(round(lon * 600000), 28)
    b.int(round(lat * 600000), 27)
    b.uint(round(cog * 10) % 3600, 12)
    b.uint(511, 9)                                  # true heading not available
    b.uint(second, 6)
    b.uint(0, 2)
    b.uint(1, 1)                                    # carrier sense unit
    b.uint(0, 1); b.uint(0, 1); b.uint(1, 1); b.uint(0, 1); b.uint(0, 1); b.uint(0, 1)
    b.uint(0b11100000000000000110, 20)              # radio status, as many class B units send
    return sentence(*b.payload())


def static_report(mmsi, name, ship_type):
    """Type 24, parts A (name) and B (ship type)."""
    a = Bits()
    a.uint(24, 6); a.uint(0, 2); a.uint(mmsi, 30); a.uint(0, 2); a.text(name, 20)
    b = Bits()
    b.uint(24, 6); b.uint(0, 2); b.uint(mmsi, 30); b.uint(1, 2); b.uint(ship_type, 8)
    b.text("NAVPLT", 3); b.uint(0, 4); b.uint(0, 20)  # vendor "NAV", model and serial unknown
    b.text("", 7)                                   # call sign
    b.uint(0, 30); b.uint(1, 4); b.uint(0, 2)       # dimensions unknown, GPS
    return [sentence(*a.payload()), sentence(*b.payload())]


class AisOutput:
    """Sends the sentences over UDP to every destination in the settings, for example Signal K and OpenCPN."""

    def __init__(self, destinations):
        self.destinations = []
        for d in destinations:
            host, _, port = d.rpartition(":")
            self.destinations.append((host or "127.0.0.1", int(port)))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sent = 0

    def send(self, sentences):
        data = "".join(s + "\r\n" for s in sentences).encode("ascii")
        for dest in self.destinations:
            try:
                self.sock.sendto(data, dest)
                self.sent += len(sentences)
            except OSError:
                pass
