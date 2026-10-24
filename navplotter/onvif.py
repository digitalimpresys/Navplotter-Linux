"""Pan and tilt of a network camera through ONVIF (Tapo C500/C510W/C200... on port 2020, with the camera account).

Only what the camera page needs: find the PTZ service and the first media profile, then move and stop. Plain SOAP
over HTTP with the WS-Security UsernameToken digest, so no extra Python package is needed on the Pi.

  ptz = Ptz("192.168.1.100", 2020, "user", "password")
  ptz.move(0.5, 0)    # right, moving until stop() or for about a second (sent again while a button is held)
  ptz.stop()
"""
import base64
import datetime
import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SOAP = "http://www.w3.org/2003/05/soap-envelope"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-soap-message-security-1.0#Base64Binary"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"
MEDIA = "http://www.onvif.org/ver10/media/wsdl"
PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"


class OnvifError(Exception):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(root, name):
    """The first element with this local name, whatever its namespace."""
    for e in root.iter():
        if _local(e.tag) == name:
            return e
    return None


def security(user, password, now):
    """The WS-Security header: digest = base64(sha1(nonce + created + password))."""
    nonce = os.urandom(16)
    created = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ('<Security s:mustUnderstand="1" xmlns="%s"><UsernameToken><Username>%s</Username>'
            '<Password Type="%s">%s</Password><Nonce EncodingType="%s">%s</Nonce>'
            '<Created xmlns="%s">%s</Created></UsernameToken></Security>'
            % (WSSE, esc(user), DIGEST, digest, B64, base64.b64encode(nonce).decode(), WSU, created))


class Ptz:
    def __init__(self, host, port=2020, user="", password="", timeout=4):
        self.device_url = "http://%s:%d/onvif/device_service" % (host, port)
        self.host, self.port = host, port
        self.user, self.password, self.timeout = user, password, timeout
        self.offset = datetime.timedelta(0)     # camera clock minus ours: the digest carries the time
        self.ptz_url = self.media_url = self.profile = None
        self.continuous = True                  # False when the camera refuses ContinuousMove: small steps instead

    def _call(self, url, body, auth=True):
        header = ""
        if auth and self.user:
            now = datetime.datetime.now(datetime.timezone.utc) + self.offset
            header = "<s:Header>%s</s:Header>" % security(self.user, self.password, now)
        data = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="%s">%s<s:Body>%s</s:Body></s:Envelope>'
                % (SOAP, header, body)).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return ET.fromstring(r.read())
        except urllib.error.HTTPError as e:
            text = e.read()
            try:
                fault = ET.fromstring(text)
                words = " ".join((x.text or "") for x in fault.iter() if _local(x.tag) in ("Value", "Text"))
                reason = _find(fault, "Text")
                reason = (reason.text or "") if reason is not None else ""
            except ET.ParseError:
                words = reason = ""
            if e.code == 401 or "authoriz" in words.lower():
                raise OnvifError("wrong user or password of the camera account") from None
            raise OnvifError("ONVIF %d %s" % (e.code, reason or e.reason)) from None
        except ET.ParseError:
            raise OnvifError("ONVIF answer not readable") from None
        except (urllib.error.URLError, OSError) as e:
            raise OnvifError("no ONVIF on %s:%d (%s)" % (self.host, self.port, getattr(e, "reason", e))) from None

    def connect(self):
        """Clock, services and media profile. Raises OnvifError when the camera has no pan and tilt."""
        try:
            r = self._call(self.device_url, '<GetSystemDateAndTime xmlns="%s"/>' % DEVICE, auth=False)
            utc = _find(r, "UTCDateTime")
            if utc is not None:
                d, t = _find(utc, "Date"), _find(utc, "Time")
                v = lambda e, n: int(_find(e, n).text)
                cam = datetime.datetime(v(d, "Year"), v(d, "Month"), v(d, "Day"), v(t, "Hour"), v(t, "Minute"),
                                        v(t, "Second"), tzinfo=datetime.timezone.utc)
                self.offset = cam - datetime.datetime.now(datetime.timezone.utc)
        except (OnvifError, AttributeError, TypeError, ValueError):
            pass                                # not all cameras answer it; our clock then
        caps = self._call(self.device_url, '<GetCapabilities xmlns="%s"><Category>All</Category></GetCapabilities>'
                          % DEVICE)
        media, ptz = _find(caps, "Media"), _find(caps, "PTZ")
        self.media_url = self._xaddr(media) or self.device_url
        self.ptz_url = self._xaddr(ptz)
        if not self.ptz_url:
            raise OnvifError("the camera has no pan and tilt (no ONVIF PTZ service)")
        profiles = self._call(self.media_url, '<GetProfiles xmlns="%s"/>' % MEDIA)
        for e in profiles.iter():
            if _local(e.tag) == "Profiles" and e.get("token"):
                self.profile = e.get("token")
                break
        if not self.profile:
            raise OnvifError("the camera has no media profile")
        return self

    def _xaddr(self, service):
        x = _find(service, "XAddr") if service is not None else None
        if x is None or not (x.text or "").strip():
            return None
        url = x.text.strip()
        # Some cameras give an address of their own that is not reachable from here: the host we know instead
        u = urllib.parse.urlparse(url)
        return u._replace(netloc="%s:%d" % (self.host, u.port or 80)).geturl()

    def move(self, x, y):
        """Pan x and tilt y in -1..1 (right and up positive)."""
        if not self.profile:
            self.connect()
        if self.continuous:
            try:
                self._call(self.ptz_url, '<ContinuousMove xmlns="%s"><ProfileToken>%s</ProfileToken><Velocity>'
                           '<PanTilt xmlns="%s" x="%.2f" y="%.2f"/></Velocity><Timeout>PT1S</Timeout>'
                           '</ContinuousMove>' % (PTZ, self.profile, SCHEMA, x, y))
                return
            except OnvifError as e:
                if not str(e).startswith("ONVIF "):     # not a refusal of the command: no network, wrong password
                    raise
                self.continuous = False
        self._call(self.ptz_url, '<RelativeMove xmlns="%s"><ProfileToken>%s</ProfileToken><Translation>'
                   '<PanTilt xmlns="%s" x="%.2f" y="%.2f"/></Translation></RelativeMove>'
                   % (PTZ, self.profile, SCHEMA, x * 0.1, y * 0.1))

    def stop(self):
        if not self.profile or not self.continuous:
            return
        self._call(self.ptz_url, '<Stop xmlns="%s"><ProfileToken>%s</ProfileToken><PanTilt>true</PanTilt>'
                   '<Zoom>false</Zoom></Stop>' % (PTZ, self.profile))

