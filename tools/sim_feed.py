"""
Live aircraft position feed for MSFS (SimConnect) and X-Plane (UDP datarefs).

Both run as daemon background threads that write into a single shared
PositionStore, so the server just polls store.get() -- it doesn't care which
sim (if either) is actually connected. Both retry forever with a backoff so
starting the server before the sim is running (or without a sim at all) is
fine, not an error.
"""
from __future__ import annotations

import logging
import math
import socket
import struct
import sys
import threading
import time

STALE_AFTER_S = 5.0


class PositionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lat: float | None = None
        self._lon: float | None = None
        self._heading: float | None = None
        self._source: str | None = None
        self._updated_at: float = 0.0

    def set(self, lat: float, lon: float, heading: float, source: str) -> None:
        with self._lock:
            self._lat, self._lon, self._heading = lat, lon, heading
            self._source = source
            self._updated_at = time.monotonic()

    def get(self) -> dict | None:
        with self._lock:
            if self._lat is None or (time.monotonic() - self._updated_at) > STALE_AFTER_S:
                return None
            return {'lat': self._lat, 'lon': self._lon, 'heading': self._heading, 'source': self._source}


def start_msfs_feed(store: PositionStore) -> None:
    def run() -> None:
        while True:
            try:
                from SimConnect import AircraftRequests, SimConnect
                # Python-SimConnect logs a "SIM def(...)" line at ERROR level
                # on every single .get() call, via logging.getLogger(__name__)
                # -- not a print(). Their RequestList.py does three separate
                # `from X import *` (SimConnect, Enum, Constants), and each of
                # those modules defines its own module-level LOGGER, so the
                # star-imports shadow each other and the LOGGER actually used
                # ends up being SimConnect.Constants's, not SimConnect.SimConnect's.
                # Setting it on the shared 'SimConnect' parent logger covers
                # all three (they're all NOTSET, so they inherit from it) and
                # sidesteps needing to know which one wins the shadowing.
                logging.getLogger('SimConnect').setLevel(logging.CRITICAL + 1)

                sm = SimConnect()
                requests = AircraftRequests(sm, _time=200)
                print("MSFS SimConnect connected", file=sys.stderr)
                while True:
                    lat = requests.get('PLANE_LATITUDE')
                    lon = requests.get('PLANE_LONGITUDE')
                    # Despite the name, Python-SimConnect returns this in
                    # radians, not degrees (confirmed in their own
                    # RequestList.py comment) -- convert before it's used
                    # as a CSS rotation degree value.
                    heading_rad = requests.get('PLANE_HEADING_DEGREES_TRUE')
                    heading = math.degrees(heading_rad) % 360 if heading_rad is not None else 0.0
                    if lat is not None and lon is not None:
                        store.set(lat, lon, heading, 'MSFS')
                    time.sleep(0.5)
            except Exception as exc:
                print(f"MSFS SimConnect not available ({exc!r}), retrying in 10s", file=sys.stderr)
                time.sleep(10)

    threading.Thread(target=run, daemon=True, name='msfs-feed').start()


# X-Plane's UDP dataref protocol (RREF): we ask it to stream specific
# datarefs to us by name; no config needed on the X-Plane side, it always
# listens on this port for incoming requests.
_XPLANE_DATAREFS = {
    1: 'sim/flightmodel/position/latitude',
    2: 'sim/flightmodel/position/longitude',
    3: 'sim/flightmodel/position/true_psi',  # aircraft true heading, degrees
}


def start_xplane_feed(store: PositionStore, host: str = '127.0.0.1', port: int = 49000) -> None:
    def run() -> None:
        while True:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind(('0.0.0.0', 0))
                sock.settimeout(STALE_AFTER_S)

                for idx, dataref in _XPLANE_DATAREFS.items():
                    packet = struct.pack('<5sii400s', b'RREF\x00', 2, idx, dataref.encode('utf-8'))
                    sock.sendto(packet, (host, port))

                print(f"Listening for X-Plane UDP datarefs from {host}:{port}", file=sys.stderr)
                values: dict[int, float] = {}
                last_resubscribe = time.monotonic()

                while True:
                    try:
                        data, _ = sock.recvfrom(2048)
                    except socket.timeout:
                        # No packets recently -- X-Plane may not be running, or
                        # may have been restarted (subscriptions don't survive
                        # a sim restart). Re-send the subscribe requests.
                        for idx, dataref in _XPLANE_DATAREFS.items():
                            packet = struct.pack('<5sii400s', b'RREF\x00', 2, idx, dataref.encode('utf-8'))
                            sock.sendto(packet, (host, port))
                        continue

                    if data[:5] != b'RREF,':
                        continue
                    body = data[5:]
                    for i in range(len(body) // 8):
                        idx, val = struct.unpack_from('<if', body, i * 8)
                        values[idx] = val

                    if 1 in values and 2 in values:
                        store.set(values[1], values[2], values.get(3, 0.0), 'X-Plane')

                    # Datarefs re-zero on aircraft reload; keep subscriptions fresh.
                    if time.monotonic() - last_resubscribe > 30:
                        last_resubscribe = time.monotonic()
                        for idx, dataref in _XPLANE_DATAREFS.items():
                            packet = struct.pack('<5sii400s', b'RREF\x00', 2, idx, dataref.encode('utf-8'))
                            sock.sendto(packet, (host, port))
            except Exception as exc:
                print(f"X-Plane UDP feed error ({exc!r}), retrying in 10s", file=sys.stderr)
                time.sleep(10)
            finally:
                if sock:
                    sock.close()

    threading.Thread(target=run, daemon=True, name='xplane-feed').start()
