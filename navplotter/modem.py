"""The Heltec V2 USB modem (firmware/heltec-modem): one text line per packet, see docs/PROTOCOL.md, section 10."""
import logging
import threading
import time

import serial

log = logging.getLogger("navplotter.modem")


class Modem:
    """Reads the modem in its own thread and calls on_packet(bytes, rssi, snr) and on_tx_done(ok, reason).

    Opening the port resets the board: nothing is sent before its READY line. If the port goes away (cable
    pulled) the modem is opened again every few seconds.
    """

    def __init__(self, port, on_packet, on_tx_done, on_ready=None):
        self.port_name = port
        self.on_packet, self.on_tx_done, self.on_ready = on_packet, on_tx_done, on_ready
        self.port = None
        self.ready = False
        self.node_id = None
        self.info = ""
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="modem", daemon=True)

    def start(self):
        self.thread.start()

    def transmit(self, data):
        """Sends one packet; the answer comes through on_tx_done. False if the modem is not there."""
        with self.lock:
            if not self.ready or self.port is None:
                return False
            try:
                self.port.write(b"TX " + data.hex().upper().encode() + b"\n")
                return True
            except (OSError, serial.SerialException):
                return False

    def _run(self):
        while True:
            try:
                self._open()
                self._read_loop()
            except (OSError, serial.SerialException) as e:
                log.warning("modem on %s: %s", self.port_name, e)
            with self.lock:
                self.ready = False
                if self.port:
                    try:
                        self.port.close()
                    except Exception:
                        pass
                self.port = None
            time.sleep(5)

    def _open(self):
        port = serial.Serial(self.port_name, 115200, timeout=1)
        with self.lock:
            self.port = port
        log.info("modem port %s open, waiting for READY", self.port_name)
        asked = time.monotonic()
        while not self.ready:
            line = port.readline().decode("ascii", "replace").strip()
            if line.startswith("READY"):
                self._got_ready(line)
            elif not line and time.monotonic() - asked > 3:
                port.write(b"INFO\n")
                asked = time.monotonic()

    def _got_ready(self, line):
        parts = line.split()
        try:
            self.node_id = int(parts[2], 16)
        except (IndexError, ValueError):
            pass
        self.info = line
        self.ready = True
        log.info("modem ready: %s", line)
        if self.on_ready:
            self.on_ready(self.node_id)

    def _read_loop(self):
        while True:
            line = self.port.readline().decode("ascii", "replace").strip()
            if not line:
                continue
            if line.startswith("RX "):
                parts = line.split()
                try:
                    data = bytes.fromhex(parts[1])
                    rssi, snr = float(parts[2]), float(parts[3])
                except (IndexError, ValueError):
                    log.debug("bad RX line: %s", line)
                    continue
                self.on_packet(data, rssi, snr)
            elif line == "TXOK":
                self.on_tx_done(True, "")
            elif line.startswith(("TXERR", "BUSY", "DUTY")):
                log.info("modem: %s", line)
                self.on_tx_done(False, line)
            elif line.startswith("READY"):
                # The board restarted by itself (or after a reset): an answer for a TX will not come
                log.warning("modem restarted: %s", line)
                self._got_ready(line)
                self.on_tx_done(False, "restart")
            elif line.startswith("#"):
                log.debug("modem %s", line)
