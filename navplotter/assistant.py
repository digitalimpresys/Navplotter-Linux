"""The answers of the voice assistant (navplotter/voice.py), in Italian, from the node's own data.

answer(text, data) -> (reply, action). The usual questions at the helm (position, speed, next waypoint, boats
nearby, collision alarm, messages, time) are answered here at once from the data the node gives its web app:
  data = {"state": /api/state, "messages": /api/messages, "userdata": /api/userdata}
Anything else goes to the local language model, if there is one (ask_llm), with the same data as context.
action is None, or ("send", to_id_hex, to_name, text) for a chat message to send.
"""
import datetime
import math
import re
import unicodedata

NM = 1852.0
SPOKEN_TYPES = {0: "barca", 1: "persona", 2: "oggetto"}
POINTS = ["nord", "nord-est", "est", "sud-est", "sud", "sud-ovest", "ovest", "nord-ovest"]

HELP = ("Puoi chiedermi: dove siamo, a che velocità andiamo, quanto manca al prossimo waypoint, "
        "chi c'è vicino, se c'è un allarme, se ci sono messaggi, che ore sono. "
        "Oppure: manda un messaggio a tutti, seguito dal testo.")


def normalize(text):
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", t)).strip()


def num(x, decimals=1):
    """A number as it is said in Italian: decimal comma, no useless zero."""
    s = ("%." + str(decimals) + "f") % x
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def distance(nm):
    if nm < 0.2:
        return "%d metri" % (round(nm * NM / 10) * 10)
    return "%s miglia" % num(nm, 1 if nm < 10 else 0)


def point(deg):
    return POINTS[int(((deg % 360) + 22.5) // 45) % 8]


def bearing_words(deg):
    return "%d gradi, verso %s" % (round(deg) % 360, point(deg))


def dist_brg(lat1, lon1, lat2, lon2):
    """Distance in nautical miles and true bearing, on the sphere."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * math.asin(min(1.0, math.sqrt(a))) * 3440.065
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return d, (math.degrees(math.atan2(y, x)) + 360) % 360


def coord(v, pos, neg):
    a = abs(v)
    d = int(a)
    m = (a - d) * 60
    return "%d gradi %s primi %s" % (d, num(m, 1), pos if v >= 0 else neg)


def duration(hours):
    mins = int(round(hours * 60))
    if mins < 60:
        return "%d minuti" % max(1, mins)
    h, m = divmod(mins, 60)
    return ("%d ore" % h if h > 1 else "un'ora") + (" e %d minuti" % m if m else "")


class Boat:
    """What answer() needs, read once from the node's JSON."""

    def __init__(self, data, now=None):
        st = data.get("state") or {}
        self.own = st.get("own") or {}
        self.fix = bool(self.own.get("fix"))
        self.targets = [t for t in st.get("targets") or [] if not t.get("stale")]
        self.alarm, self.alarm_name = bool(st.get("alarm")), st.get("alarmName") or ""
        self.messages = (data.get("messages") or {}).get("messages") or []
        ud = data.get("userdata") or {}
        self.wps = {w.get("id"): w for w in ud.get("wps") or []}
        self.dest = self.wps.get(ud.get("dest"))
        self.route = next((r for r in ud.get("routes") or [] if r.get("id") == ud.get("route")), None)
        self.now = now or datetime.datetime.now()

    def route_remaining(self):
        """Miles from here to the destination waypoint and then along the rest of the active route."""
        if not (self.route and self.dest and self.fix):
            return None, 0
        ids = self.route.get("wps") or []
        if self.dest.get("id") not in ids:
            return None, 0
        pts = [self.wps[i] for i in ids[ids.index(self.dest["id"]):] if i in self.wps]
        total = dist_brg(self.own["lat"], self.own["lon"], pts[0]["lat"], pts[0]["lon"])[0]
        for a, b in zip(pts, pts[1:]):
            total += dist_brg(a["lat"], a["lon"], b["lat"], b["lon"])[0]
        return total, len(pts)

    def context(self):
        """The same data in a few plain lines, for the language model."""
        lines = ["Ora locale: %s." % self.now.strftime("%H:%M")]
        if self.fix:
            lines.append("Posizione: %s, %s. Velocità %s nodi, rotta %d gradi." % (
                coord(self.own["lat"], "nord", "sud"), coord(self.own["lon"], "est", "ovest"),
                num(self.own.get("sog", 0)), round(self.own.get("cog", 0))))
        else:
            lines.append("Posizione GPS non disponibile.")
        if self.dest and self.fix:
            d, b = dist_brg(self.own["lat"], self.own["lon"], self.dest["lat"], self.dest["lon"])
            lines.append("Prossimo waypoint: %s, a %s, direzione %d gradi." % (self.dest.get("name"), distance(d), round(b)))
        for t in sorted(self.targets, key=lambda t: t.get("dist", 99))[:5]:
            lines.append("Vicino: %s (%s) a %s, direzione %d gradi, velocità %s nodi%s." % (
                t.get("name"), SPOKEN_TYPES.get(t.get("type"), "nodo"), distance(t.get("dist", 0)), t.get("brg", 0),
                num(t.get("sog", 0)), ", IN ALLARME COLLISIONE" if t.get("alarm") else ""))
        if not self.targets:
            lines.append("Nessuna barca vicina in vista.")
        for m in self.messages[-5:]:
            if not m.get("mine"):
                lines.append("Messaggio da %s: %s" % (m.get("name"), m.get("text")))
        return "\n".join(lines)


def answer(text, data, now=None):
    q = normalize(text)
    b = Boat(data, now)
    if not q:
        return "Non ho sentito niente.", None

    m = re.search(r"\b(?:manda|invia|scrivi|spedisci)\w*\s+(?:un\s+)?messaggio\s*(?:a\s+(tutti|\w+))?\s*(?:che\s+dice\s+|dicendo\s+)?(.*)$", q)
    if m:
        return send_message(text, m.group(1), b)

    if re.search(r"\b(aiuto|cosa (posso|puoi|sai)|che cosa (posso|puoi|sai)|comandi)\b", q):
        return HELP, None
    if re.search(r"\b(che or[ae]|che ora e|ora e|orario)\b", q):
        return "Sono le %s." % b.now.strftime("%H:%M"), None
    if re.search(r"\b(allarm\w*|collision\w*|pericol\w*)\b", q):
        return alarm(b), None
    if re.search(r"\b(messagg\w*|chat|scritto)\b", q):
        return messages(b), None
    if re.search(r"\b(waypoint|way point|destinazione|arriv\w*|manca\w*|prossim\w* punto|tappa|rotta attiva|eta)\b", q):
        return waypoint(b), None
    if re.search(r"\b(vicin\w*|barche|barca|chi c'e|chi ce|traffico|bersagl\w*|intorno|nodi vicini)\b", q):
        return nearby(b), None
    if re.search(r"\b(dove siamo|dove sono|posizion\w*|coordinat\w*|latitudin\w*|longitudin\w*)\b", q):
        return position(b), None
    if re.search(r"\b(velocita|veloci|quanto andiamo|quanti nodi|andatura|rotta|direzione|prua|cog|sog)\b", q):
        return speed(b), None
    return None, None


def position(b):
    if not b.fix:
        return "Il GPS non ha ancora la posizione."
    return "Siamo a %s, %s." % (coord(b.own["lat"], "nord", "sud"), coord(b.own["lon"], "est", "ovest"))


def speed(b):
    if not b.fix:
        return "Il GPS non ha ancora la posizione."
    sog = b.own.get("sog", 0)
    if sog < 0.3:
        return "Siamo fermi."
    if b.own.get("cogValid", True):
        return "Andiamo a %s nodi, rotta %s." % (num(sog), bearing_words(b.own.get("cog", 0)))
    return "Andiamo a %s nodi." % num(sog)


def waypoint(b):
    if not b.dest:
        return "Non c'è un waypoint di destinazione. Sceglilo nella pagina Waypoint con Vai."
    if not b.fix:
        return "La destinazione è %s, ma il GPS non ha ancora la posizione." % b.dest.get("name")
    d, brg = dist_brg(b.own["lat"], b.own["lon"], b.dest["lat"], b.dest["lon"])
    out = "%s è a %s, %s." % (b.dest.get("name"), distance(d), bearing_words(brg))
    sog = b.own.get("sog", 0)
    if sog >= 0.5:
        out += " A %s nodi ci arriviamo in %s, verso le %s." % (
            num(sog), duration(d / sog), (b.now + datetime.timedelta(hours=d / sog)).strftime("%H:%M"))
    total, n = b.route_remaining()
    if total is not None and n > 1:
        out += " Fino alla fine della rotta %s mancano %s, con %d waypoint." % (b.route.get("name") or "", distance(total), n)
    return out


def nearby(b):
    if not b.targets:
        return "Non vedo nessuno vicino."
    ts = sorted(b.targets, key=lambda t: t.get("dist", 99))
    n = len(ts)
    out = ("C'è un solo nodo vicino. " if n == 1 else "Ci sono %d nodi vicini. Il più vicino è " % n)
    t = ts[0]
    out += "%s, a %s, direzione %s" % (t.get("name"), distance(t.get("dist", 0)), bearing_words(t.get("brg", 0)))
    out += (", fermo." if t.get("sog", 0) < 0.3 else ", a %s nodi." % num(t.get("sog", 0)))
    alarms = [x for x in ts if x.get("alarm")]
    if alarms:
        out += " Attenzione: allarme collisione con %s." % ", ".join(x.get("name") for x in alarms)
    return out


def alarm(b):
    al = [t for t in b.targets if t.get("alarm")]
    if not (b.alarm or al):
        return "Nessun allarme."
    t = al[0] if al else None
    if not t:
        return "Allarme collisione con %s." % (b.alarm_name or "un nodo")
    out = "Allarme collisione con %s, a %s, direzione %s." % (t.get("name"), distance(t.get("dist", 0)), bearing_words(t.get("brg", 0)))
    if t.get("cpaValid") and t.get("tcpa", 0) > 0:
        out += " Passaggio più vicino a %s tra %s." % (distance(t.get("cpa", 0)), duration(t["tcpa"] / 3600))
    return out


def messages(b):
    unread = [m for m in b.messages if m.get("unread") and not m.get("mine")]
    if unread:
        head = "Hai un messaggio nuovo. " if len(unread) == 1 else "Hai %d messaggi nuovi. " % len(unread)
        return head + " ".join("Da %s: %s." % (m.get("name"), m.get("text")) for m in unread[-5:])
    incoming = [m for m in b.messages if not m.get("mine")]
    if not incoming:
        return "Nessun messaggio."
    m = incoming[-1]
    return "Nessun messaggio nuovo. L'ultimo è di %s: %s." % (m.get("name"), m.get("text"))


def send_message(original, to, b):
    # The text as it was said, without the command words in front
    o = re.sub(r"^.*?\bmessaggio\b\s*", "", original, count=1, flags=re.I)
    if to and to != "tutti":
        o = re.sub(r"^a\s+\S+\s*", "", o, count=1, flags=re.I)
    elif to == "tutti":
        o = re.sub(r"^a\s+tutti\s*", "", o, count=1, flags=re.I)
    o = re.sub(r"^(che dice|dicendo)\s+", "", o, flags=re.I).strip(" :,.")
    if not o:
        return "Cosa devo scrivere? Dimmi, per esempio: manda un messaggio a tutti, arriviamo alle sei.", None
    o = o[:63]
    if to and to != "tutti":
        t = next((x for x in b.targets if normalize(x.get("name", "")) == to), None) or \
            next((x for x in b.targets if normalize(x.get("name", "")).startswith(to)), None)
        if not t:
            return "Non trovo %s tra i nodi vicini. Il messaggio non è partito." % to, None
        return "Mando a %s: %s." % (t.get("name"), o), ("send", t.get("id"), t.get("name"), o)
    return "Mando a tutti: %s." % o, ("send", "00000000", "", o)


SYSTEM = ("Sei l'assistente di bordo di NavPlotter, su una barca a vela. Rispondi in italiano, in una o due frasi "
          "brevi da leggere ad alta voce, senza elenchi né simboli. Usa solo i dati qui sotto; se non bastano, "
          "dillo. Non inventare posizioni, distanze o meteo.\n\nDati di bordo:\n")


def llm_messages(text, data, now=None):
    return [{"role": "system", "content": SYSTEM + Boat(data, now).context()},
            {"role": "user", "content": text}]
