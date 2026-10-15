"""Settings, kept in one JSON file (by default /var/lib/navplotter/settings.json).

The web app changes the node part (name, type, alarm...); the rest is edited by hand and read at start-up.
"""
import copy
import json
import os

DEFAULTS = {
    "name": "",                     # empty: RPI-<last 4 digits of the id>
    "type": 0,                      # 0 boat, 1 person, 2 object
    "share": True,                  # transmit our own position
    "alarmRange": 3,
    "alarmProfile": 1,
    "alarmCpa": 0.1,
    "alarmTcpa": 5,
    "lang": "it",
    "tiles": 0,                     # 0 online, 1 tiles folder on the Raspberry, 2 none
    "sound": True,
    "id": "",                       # node id in hexadecimal; empty: the modem's
    "modem": "/dev/ttyUSB0",
    "http_port": 80,
    "gps": {"source": "auto", "signalk_url": "http://127.0.0.1:3000", "signalk_token": "",
            "gpsd_host": "127.0.0.1", "gpsd_port": 2947, "lat": 0.0, "lon": 0.0},
    "ais": {"enabled": True, "destinations": ["127.0.0.1:10110"], "mmsi_prefix": 999, "mmsi": {},
            "every_s": 10, "static_every_s": 60, "include_simulated": False},
    "tiles_dir": "/var/lib/navplotter/tiles",
    "data_dir": "/var/lib/navplotter",
}


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


class Settings:
    def __init__(self, path):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        try:
            with open(path) as f:
                _merge(self.data, json.load(f))
        except FileNotFoundError:
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)
