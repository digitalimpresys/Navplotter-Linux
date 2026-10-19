"""python3 -m navplotter [--settings FILE]: the NavPlotter node for Linux."""
import argparse
import logging
import os
import time

from . import web
from .gps import GpsSource
from .modem import Modem
from .node import VERSION, Node
from .settings import Settings

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(prog="navplotter", description="NavPlotter node for Linux")
    ap.add_argument("--settings", default="/var/lib/navplotter/settings.json")
    ap.add_argument("--web", default=os.path.join(os.path.dirname(HERE), "web"), help="folder with index.html")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = Settings(args.settings)
    if not os.path.exists(args.settings):
        settings.save()     # so that there is a file to edit
    node = Node(settings)
    node.modem = Modem(settings["modem"], node.on_packet, node.on_tx_done, node.on_modem_ready)
    node.modem.start()
    GpsSource(settings["gps"], node.on_fix, settings["modem"])
    web.start(node, args.web, settings["http_port"])
    logging.getLogger("navplotter").info("NavPlotter Linux %s started", VERSION)
    while True:
        node.tick()
        time.sleep(0.05)


if __name__ == "__main__":
    main()
