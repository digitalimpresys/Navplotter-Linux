"""Short text messages over LoRa, as in nav_messages.h of the firmware (docs/PROTOCOL.md, section 4)."""
import random
from dataclasses import dataclass

from . import protocol as P
from .mesh import MSG_DEDUP_MARK, RELAY_MAX_S, RELAY_MIN_S

RETRY_S = 7.0           # a message to one node is sent again this often (plus up to 1.5 s)...
TRIES = 4               # ...this many times at most, until it is confirmed
KEEP = 12               # messages kept in memory

INCOMING, PENDING, SENT, DELIVERED, READ, NO_ANSWER = range(6)
EVENT_NEW, EVENT_STATUS = 1, 2


@dataclass
class ChatMessage:
    mine: bool
    to_all: bool
    peer: int
    peer_name: str
    text: str
    number: int
    status: int
    time: float
    unread: bool = False
    tries: int = 0
    next_try: float = 0.0


class Chat:
    def __init__(self, seen):
        self.seen = seen                # the mesh SeenTable shared with the positions
        self.messages = []
        self.control = []               # confirmations: [due, packet]
        self.relays = []                # other nodes' packets: [due, bytes]
        self.next_seq = random.randrange(256)
        self.next_number = random.randrange(256)

    def _trim(self):
        del self.messages[:-KEEP]

    def _packet(self, me, name, kind, to, ref, text=""):
        pkt = P.Message(P.MESH_TTL, me, self.next_seq, kind, to, ref, name, text)
        self.next_seq = (self.next_seq + 1) & 0xFF
        return pkt.pack()

    def _queue_control(self, kind, to, ref, delay, now):
        if len(self.control) >= 6:
            self.control.pop(0)
        self.control.append([now + delay, (kind, to, ref)])

    def send(self, to, to_name, text, now):
        """Writes a message to one node (to != 0) or to everybody (to == 0)."""
        self.messages.append(ChatMessage(True, to == 0, to, to_name[:P.NAME_LEN - 1], P.clean_text(text)[:P.MSG_TEXT_LEN - 1],
                                         self.next_number, PENDING, now, next_try=now))
        self.next_number = (self.next_number + 1) & 0xFF
        self._trim()

    def next_packet(self, me, name, now):
        """The radio is free: the next chat packet to transmit, or None."""
        for q in self.control:
            if now >= q[0]:
                self.control.remove(q)
                kind, to, ref = q[1]
                return self._packet(me, name, kind, to, ref)
        for m in self.messages:
            if not m.mine or m.status != PENDING or now < m.next_try:
                continue
            if m.tries >= (2 if m.to_all else TRIES):
                m.status = SENT if m.to_all else NO_ANSWER
                continue
            m.tries += 1
            m.next_try = now + RETRY_S + random.uniform(0, 1.5)
            return self._packet(me, name, P.MSG_TEXT, m.peer, m.number, m.text)
        for r in self.relays:
            if now >= r[0]:
                self.relays.remove(r)
                return r[1]
        return None

    def receive(self, me, name, data, now):
        """A packet starting with 'N','M'. Returns EVENT_* bits."""
        pkt = P.unpack_message(data)
        if pkt is None or pkt.id == me:
            return 0
        for_me, for_all = pkt.to == me, pkt.to == 0
        events = 0
        # The same transmission heard again: only a text addressed to us is looked at twice, because its sender may
        # be repeating it for want of our confirmation
        seen = self.seen.already_seen(pkt.id ^ MSG_DEDUP_MARK, pkt.seq, now)

        if pkt.kind == P.MSG_TEXT and (for_me or for_all):
            known = next((m for m in reversed(self.messages) if not m.mine and m.peer == pkt.id and m.number == pkt.ref), None)
            if known is None:
                self.messages.append(ChatMessage(False, for_all, pkt.id, pkt.name, pkt.text, pkt.ref, INCOMING, now, unread=True))
                self._trim()
                events |= EVENT_NEW
                if for_me:
                    self._queue_control(P.MSG_RECEIVED, pkt.id, pkt.ref, random.uniform(0.2, 0.6), now)
            elif for_me and not seen:
                # A repeat: our confirmation got lost, say it again
                kind = P.MSG_RECEIVED if known.unread else P.MSG_READ
                self._queue_control(kind, pkt.id, pkt.ref, random.uniform(0.2, 0.6), now)
        elif pkt.kind in (P.MSG_RECEIVED, P.MSG_READ) and for_me:
            status = READ if pkt.kind == P.MSG_READ else DELIVERED
            for m in self.messages:
                if m.mine and not m.to_all and m.peer == pkt.id and m.number == pkt.ref and m.status not in (READ, status):
                    m.status = status
                    events |= EVENT_STATUS

        # Mesh: pass on what is not for us alone
        if not seen and not for_me and pkt.ttl > 0 and len(self.relays) < 3:
            self.relays.append([now + random.uniform(RELAY_MIN_S, RELAY_MAX_S), P.with_ttl(data, pkt.ttl - 1)])
        return events

    def mark_read(self, me, name, index, now):
        m = self.messages[index]
        if m.mine or not m.unread:
            return
        m.unread = False
        if m.to_all:
            return
        # Twice, a few seconds apart, because this confirmation is not repeated on request
        self._queue_control(P.MSG_READ, m.peer, m.number, 0.1, now)
        self._queue_control(P.MSG_READ, m.peer, m.number, 4.0, now)

    def mark_conversation_read(self, me, name, peer, all_, now):
        n = 0
        for i, m in enumerate(self.messages):
            if m.mine or not m.unread or m.to_all != all_ or (not all_ and m.peer != peer):
                continue
            self.mark_read(me, name, i, now)
            n += 1
        return n

    def unread_count(self):
        return sum(1 for m in self.messages if m.unread)

    def stamp(self):
        """Changes whenever anything in the list changes: the web app asks for the list only then."""
        h = len(self.messages)
        for m in self.messages:
            h = (h * 31 + m.number * 7 + m.status * 3 + m.tries + (1 if m.unread else 0) + m.peer) & 0xFFFFFFFF
        return h

    def to_json(self, now):
        return {"stamp": self.stamp(), "messages": [
            {"mine": m.mine, "all": m.to_all, "peer": "%08X" % m.peer, "status": m.status, "unread": m.unread,
             "age": int(now - m.time), "name": m.peer_name, "text": m.text} for m in self.messages]}
