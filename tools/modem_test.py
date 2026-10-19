#!/usr/bin/env python3
"""Prova del modem NavPlotter (Heltec V2 via USB) dal Raspberry Pi.

Mostra i pacchetti ricevuti (posizioni e chat) e, se gli si danno nome e posizione, trasmette ogni 10 s
una posizione come un nodo NavPlotter, cosi' il Raspberry compare sul T-Deck e sugli altri nodi.
Non ritrasmette i pacchetti degli altri (niente mesh): e' solo una prova della radio.

    python3 tools/modem_test.py --port /dev/ttyUSB0
    python3 tools/modem_test.py --port /dev/ttyUSB0 --name RASPI --lat 45.4637 --lon 12.1471

Serve pyserial (sudo apt install python3-serial). Formati in docs/PROTOCOL.md.
"""
import argparse
import struct
import sys
import time

import serial

POS = struct.Struct("<2sBBIBBiiHH12s")   # 34 byte, pacchetto 'N','P'
MSG_HEADER = struct.Struct("<2sBBIBBIB12s")  # 27 byte, poi il testo del pacchetto 'N','M'
NODE_TYPES = {0: "barca", 1: "persona", 2: "oggetto"}
MSG_KINDS = {0: "testo", 1: "ricevuto", 2: "letto"}


def text(raw):
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def describe(data):
    if len(data) == POS.size and data[:2] == b"NP":
        _, ver, ttl, node, seq, flags, lat, lon, cog, sog, name = POS.unpack(data)
        kind = NODE_TYPES.get((flags >> 1) & 3, "barca")
        return (f"POS v{ver} {text(name)!r} id {node:08X} seq {seq} ttl {ttl} {kind}"
                f"{' simulata' if flags & 1 else ''} {lat / 1e7:.5f} {lon / 1e7:.5f}"
                f" rotta {cog / 10:.1f} vel {sog / 100:.2f} kn")
    if MSG_HEADER.size < len(data) <= MSG_HEADER.size + 64 and data[:2] == b"NM":
        _, ver, ttl, node, seq, kind, to, ref, name = MSG_HEADER.unpack(data[:MSG_HEADER.size])
        dest = "tutti" if to == 0 else f"{to:08X}"
        return (f"MSG v{ver} {MSG_KINDS.get(kind, kind)} da {text(name)!r} ({node:08X}) a {dest}"
                f" n.{ref} seq {seq} ttl {ttl}: {text(data[MSG_HEADER.size:])!r}")
    return f"sconosciuto, {len(data)} byte"


def wait_ready(port):
    # Aprendo la porta la Heltec si riavvia: si aspetta la riga READY
    deadline = time.time() + 8
    while time.time() < deadline:
        line = port.readline().decode("ascii", "replace").strip()
        if line.startswith("READY"):
            return line
        if not line:
            port.write(b"INFO\n")
    sys.exit("Il modem non risponde: firmware caricato? porta giusta?")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--name", help="nome da trasmettere (11 caratteri al massimo)")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--every", type=float, default=10.0, help="secondi tra una posizione e l'altra")
    args = ap.parse_args()
    send = args.name is not None
    if send and (args.lat is None or args.lon is None):
        ap.error("per trasmettere servono --name, --lat e --lon")

    port = serial.Serial(args.port, 115200, timeout=1)
    ready = wait_ready(port)
    print(ready)
    node_id = int(ready.split()[2], 16)
    seq, next_tx = 0, time.time() + 2

    while True:
        if send and time.time() >= next_tx:
            name = args.name.encode("ascii", "replace")[:11].ljust(12, b"\0")
            pkt = POS.pack(b"NP", 1, 2, node_id, seq, 0, round(args.lat * 1e7), round(args.lon * 1e7), 0, 0, name)
            port.write(b"TX " + pkt.hex().upper().encode() + b"\n")
            print(f"TX posizione seq {seq}")
            seq = (seq + 1) % 256
            next_tx = time.time() + args.every
        line = port.readline().decode("ascii", "replace").strip()
        if not line:
            continue
        if line.startswith("RX "):
            parts = line.split()
            try:
                data = bytes.fromhex(parts[1])
            except (IndexError, ValueError):
                print("riga RX non valida:", line)
                continue
            print(f"RX rssi {parts[2]} snr {parts[3]}  {describe(data)}")
        else:
            print(line)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
