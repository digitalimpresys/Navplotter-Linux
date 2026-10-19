"""Mesh: copies already seen and relays waiting to go out, as in traffic_packet.h."""
import random

SEEN_SIZE = 24
SEEN_EXPIRE_S = 60.0
RELAY_MIN_S, RELAY_MAX_S = 0.3, 1.5
MSG_DEDUP_MARK = 0x4D534721     # keeps chat numbers apart from position numbers in the seen table


class SeenTable:
    """One row per node id with its last number: a packet is "seen" if its number is that one or one of the 15 before."""

    def __init__(self):
        self.rows = {}      # id -> (seq, time)

    def already_seen(self, nid, seq, now):
        row = self.rows.get(nid)
        if row and now - row[1] < SEEN_EXPIRE_S and (row[0] - seq) & 0xFF < 16:
            return True
        if row is None and len(self.rows) >= SEEN_SIZE:
            del self.rows[min(self.rows, key=lambda k: self.rows[k][1])]
        self.rows[nid] = (seq, now)
        return False


class RelayQueue:
    """Packets of other nodes waiting to be relayed, each after a random delay."""

    def __init__(self, size):
        self.size = size
        self.items = []     # [due, id, seq, data]

    def add(self, nid, seq, data, now):
        if len(self.items) >= self.size:
            return False
        self.items.append([now + random.uniform(RELAY_MIN_S, RELAY_MAX_S), nid, seq, data])
        return True

    def cancel(self, nid, seq):
        self.items = [i for i in self.items if not (i[1] == nid and i[2] == seq)]

    def next_due(self, now):
        for i in self.items:
            if now >= i[0]:
                self.items.remove(i)
                return i[3]
        return None
