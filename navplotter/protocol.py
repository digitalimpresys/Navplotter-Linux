"""NavPlotter LoRa packets, as in traffic_packet.h and nav_messages.h of the firmware (docs/PROTOCOL.md)."""
import struct
from dataclasses import dataclass

MESH_TTL = 2
NAME_LEN = 12            # 11 characters + terminator
MSG_TEXT_LEN = 64        # 63 characters + terminator

FLAG_SIMULATED = 0x01
TYPE_SHIFT, TYPE_MASK = 1, 0x03
SLEEP_SHIFT, SLEEP_MASK = 3, 0x07
SLEEP_MINUTES = (0, 1, 2, 5, 10, 15, 30, 60)
NODE_BOAT, NODE_PERSON, NODE_OBJECT = 0, 1, 2

MSG_TEXT, MSG_RECEIVED, MSG_READ = 0, 1, 2

_POS = struct.Struct("<2sBBIBBiiHH12s")      # 34 bytes
_MSG = struct.Struct("<2sBBIBBIB12s")        # 27 bytes, then the text
POS_LEN = _POS.size
MSG_HEADER_LEN = _MSG.size


def clean_name(raw):
    """A name from the air: up to the terminator, plain printable characters only, as the nodes do."""
    text = raw.split(b"\0", 1)[0][:NAME_LEN - 1].decode("latin-1")
    return "".join(c if " " <= c <= "~" and c not in '"\\<>&' else "?" for c in text)


def clean_text(text):
    """Only plain printable characters travel in a message."""
    return "".join(c if " " <= c <= "~" else "?" for c in text)


def _name_bytes(name):
    return name.encode("ascii", "replace")[:NAME_LEN - 1].ljust(NAME_LEN, b"\0")


@dataclass
class Position:
    ttl: int
    id: int
    seq: int
    flags: int
    lat: float          # degrees
    lon: float
    cog: float          # degrees
    sog: float          # knots
    name: str

    @property
    def node_type(self):
        t = (self.flags >> TYPE_SHIFT) & TYPE_MASK
        return t if t <= NODE_OBJECT else NODE_BOAT

    @property
    def sleep_minutes(self):
        return SLEEP_MINUTES[(self.flags >> SLEEP_SHIFT) & SLEEP_MASK]

    def pack(self):
        return _POS.pack(b"NP", 1, self.ttl, self.id & 0xFFFFFFFF, self.seq & 0xFF, self.flags & 0xFF,
                         round(self.lat * 1e7), round(self.lon * 1e7), round(self.cog * 10) % 3600,
                         min(round(self.sog * 100), 0xFFFF), _name_bytes(self.name))


def unpack_position(data):
    """A position packet, or None. Same checks as the nodes: 34 bytes, 'N','P', version 1."""
    if len(data) != POS_LEN:
        return None
    magic, ver, ttl, nid, seq, flags, lat, lon, cog, sog, name = _POS.unpack(data)
    if magic != b"NP" or ver != 1:
        return None
    return Position(ttl, nid, seq, flags, lat / 1e7, lon / 1e7, cog / 10.0, sog / 100.0, clean_name(name))


@dataclass
class Message:
    ttl: int
    id: int
    seq: int
    kind: int
    to: int
    ref: int
    name: str
    text: str = ""

    def pack(self):
        head = _MSG.pack(b"NM", 1, self.ttl, self.id & 0xFFFFFFFF, self.seq & 0xFF, self.kind, self.to & 0xFFFFFFFF,
                         self.ref & 0xFF, _name_bytes(self.name))
        return head + self.text.encode("ascii", "replace")[:MSG_TEXT_LEN - 1] + b"\0"


def unpack_message(data):
    """A chat packet, or None: 28 to 91 bytes, 'N','M', version 1."""
    if not MSG_HEADER_LEN + 1 <= len(data) <= MSG_HEADER_LEN + MSG_TEXT_LEN or data[:2] != b"NM":
        return None
    magic, ver, ttl, nid, seq, kind, to, ref, name = _MSG.unpack(data[:MSG_HEADER_LEN])
    if ver != 1:
        return None
    text = data[MSG_HEADER_LEN:].split(b"\0", 1)[0][:MSG_TEXT_LEN - 1].decode("latin-1")
    return Message(ttl, nid, seq, kind, to, ref, clean_name(name), clean_text(text))


def with_ttl(data, ttl):
    """The same packet with another ttl (byte 3), for relaying."""
    return data[:3] + bytes([ttl]) + data[4:]
