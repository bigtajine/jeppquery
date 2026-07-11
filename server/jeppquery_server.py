#!/usr/bin/env python3
"""
JeppQuery API server.

Successor to the old "Marinvent" chart API. Fixes two problems found in the
original compiled server against a current (81,917-record) charts.dbf:

  1. /api/v1/charts/{icao} took ~17s per request (linear scan of charts.dbf
     on every call). This version indexes charts.dbf/vfrchrts.dbf by ICAO
     once at startup, so lookups are O(1).
  2. The old README documented PORT/HOST env vars, but the compiled binary
     only read -port/-host flags. This version honors both (flag wins).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if getattr(sys, 'frozen', False):
    # Under PyInstaller onefile, __file__ points into a temp extraction dir,
    # not the real exe location. The exe lives in <repo root>/bin/.
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
    REPO_ROOT = os.path.dirname(SCRIPT_DIR)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    REPO_ROOT = os.path.dirname(SCRIPT_DIR)
    sys.path.insert(0, os.path.join(REPO_ROOT, 'tools'))

from dbf import DbfFile  # noqa: E402
from pdf_fixup_threshold import process_pdf  # noqa: E402

FILENAME_RE = re.compile(r'^[A-Za-z0-9_]+$')
CHART_DIMS_RE = re.compile(r'Chart:\s*(\d+)\s*x\s*(\d+)')

DEFAULT_CHARTS_DBF = r'C:\ProgramData\Jeppesen\Common\TerminalCharts\charts.dbf'
DEFAULT_VFR_DBF = r'C:\ProgramData\Jeppesen\Common\TerminalCharts\vfrchrts.dbf'
DEFAULT_TYPES_DBF = r'C:\ProgramData\Jeppesen\Common\TerminalCharts\ctypes.dbf'
DEFAULT_AIRPORTS_DBF = r'C:\ProgramData\Jeppesen\Common\TerminalCharts\Airports.dbf'
DEFAULT_TCL_DIR = os.path.join(REPO_ROOT, 'TCLs')


class ChartCatalog:
    def __init__(self, charts_dbf: str, vfr_dbf: str, types_dbf: str, tcl_dir: str) -> None:
        self.tcl_dir = os.path.abspath(tcl_dir)
        self.tcl2emf_exe = os.path.join(REPO_ROOT, 'bin', 'tcl2emf.exe')
        self.fixup_script = os.path.join(REPO_ROOT, 'tools', 'pdf_fixup_threshold.py')
        self.cache_dir = os.path.join(REPO_ROOT, 'cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        self.chart_dims: dict[str, tuple[int, int]] = {}

        self._load_types(types_dbf)
        self._load_tcl_names(tcl_dir)
        self.charts_by_icao: dict[str, list[dict]] = {}
        self._load_charts(charts_dbf, is_vfr=False)
        self._load_charts(vfr_dbf, is_vfr=True)

    def _load_types(self, path: str) -> None:
        self.types_by_code: dict[str, dict] = {}
        self.types_list: list[dict] = []
        with open(path, 'rb') as fd:
            header, fields = DbfFile.read_header(fd)
            for _ in range(header.num_records):
                code, category, name, precision = DbfFile.read_record(fd, fields)
                entry = {
                    'code': code,
                    'category': category,
                    'name': name,
                    'precision': precision,
                }
                self.types_by_code[code] = entry
                self.types_list.append(entry)

    def _load_tcl_names(self, tcl_dir: str) -> None:
        self.tcl_names: set[str] = set()
        if os.path.isdir(tcl_dir):
            for name in os.listdir(tcl_dir):
                base, ext = os.path.splitext(name)
                if ext.upper() == '.TCL':
                    self.tcl_names.add(base.upper())

    def _load_charts(self, path: str, is_vfr: bool) -> None:
        if not os.path.exists(path):
            print(f"WARNING: {path} not found, skipping", file=sys.stderr)
            return
        with open(path, 'rb') as fd:
            header, fields = DbfFile.read_header(fd)
            for _ in range(header.num_records):
                icao, filename, chart_type, index_no, proc_id, action, date_rev, date_eff, \
                    trim_size, georef, sheet_id, ft_bk = DbfFile.read_record(fd, fields)

                type_entry = self.types_by_code.get(chart_type)
                record = {
                    'filename': filename,
                    'icao': icao,
                    'chart_type': chart_type,
                    'type_name': type_entry['name'] if type_entry else '',
                    'category': type_entry['category'] if type_entry else '',
                    'proc_id': proc_id,
                    'date_eff': date_eff.isoformat() if date_eff else '',
                    'sheet_id': sheet_id,
                    'has_tcl': filename.upper() in self.tcl_names,
                    'is_vfr': is_vfr,
                }
                self.charts_by_icao.setdefault(icao, []).append(record)

    def get_charts(self, icao: str, type_filter: str | None, search: str | None) -> list[dict]:
        records = self.charts_by_icao.get(icao.upper(), [])

        if type_filter:
            tf_upper = type_filter.upper()
            if tf_upper in self.types_by_code:
                matching_codes = {tf_upper}
            else:
                matching_codes = {
                    code for code, entry in self.types_by_code.items()
                    if tf_upper in entry['category'].upper() or tf_upper in entry['name'].upper()
                }
            records = [r for r in records if r['chart_type'] in matching_codes]

        if search:
            search_upper = search.upper()
            records = [r for r in records if search_upper in r['proc_id'].upper()]

        return records

    def has_chart(self, icao: str, filename: str) -> bool:
        return any(r['filename'] == filename for r in self.charts_by_icao.get(icao.upper(), []))

    def get_chart_data(self, icao: str, filename: str) -> dict:
        """Chart dimensions come from tcl2emf.exe's own stdout ("Chart: W x
        H"), so they're only known after the chart has been rendered at
        least once. Not georeferenced yet — georef_tool.exe isn't ported."""
        width, height = self.chart_dims.get(filename, (0, 0))
        return {
            'filename': filename,
            'icao': icao.upper(),
            'has_tcl': filename.upper() in self.tcl_names,
            'width': width,
            'height': height,
            'georef': {'georeferenced': False},
        }

    def export_pdf(self, filename: str) -> bytes | None:
        """Render a TCL chart to PDF via tcl2emf.exe, with waypoint-overlay
        cleanup, caching the result on disk since rendering isn't free."""
        if not FILENAME_RE.match(filename):
            return None

        cache_path = os.path.join(self.cache_dir, f'{filename}.pdf')
        if os.path.exists(cache_path):
            with open(cache_path, 'rb') as fd:
                return fd.read()

        tcl_path = os.path.join(self.tcl_dir, f'{filename}.TCL')
        if not os.path.exists(tcl_path):
            return None

        result = subprocess.run(
            [self.tcl2emf_exe, tcl_path, cache_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"tcl2emf failed for {filename}: rc={result.returncode!r} stdout={result.stdout!r} stderr={result.stderr!r}", file=sys.stderr)
            return None

        dims_match = CHART_DIMS_RE.search(result.stdout)
        if dims_match:
            self.chart_dims[filename] = (int(dims_match.group(1)), int(dims_match.group(2)))

        # tcl2emf.exe renders via the "Microsoft Print to PDF" printer driver,
        # which writes the file through the print spooler asynchronously
        # after the process has already exited.
        deadline = time.monotonic() + 15
        while not os.path.exists(cache_path):
            if time.monotonic() > deadline:
                print(f"tcl2emf produced no file for {filename} within timeout", file=sys.stderr)
                return None
            time.sleep(0.02)
        # Give the spooler a moment to finish flushing the file handle.
        prev_size = -1
        for _ in range(100):
            size = os.path.getsize(cache_path)
            if size == prev_size and size > 0:
                break
            prev_size = size
            time.sleep(0.02)

        # In-process instead of shelling out: avoids spawning a second
        # interpreter (was also silently broken in the frozen exe, since
        # sys.executable there is jeppquery-server.exe itself, not python).
        try:
            process_pdf(cache_path, dry_run=False)
        except Exception as exc:
            print(f"pdf fixup failed for {filename}: {exc!r}", file=sys.stderr)
        bak_path = cache_path[:-4] + '.bak'
        if os.path.exists(bak_path):
            os.remove(bak_path)

        with open(cache_path, 'rb') as fd:
            return fd.read()


def make_handler(catalog: ChartCatalog):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            start = time.monotonic()
            parsed = urllib.parse.urlsplit(self.path)
            parts = [p for p in parsed.path.split('/') if p]
            qs = urllib.parse.parse_qs(parsed.query)

            try:
                if parsed.path == '/health':
                    self._json(200, {'status': 'ok'})
                elif parts == ['api', 'v1', 'chart-types']:
                    self._json(200, {'types': catalog.types_list})
                elif len(parts) == 4 and parts[:2] == ['api', 'v1'] and parts[2] == 'charts':
                    icao = parts[3]
                    charts = catalog.get_charts(icao, qs.get('type', [None])[0], qs.get('search', [None])[0])
                    self._json(200, {'icao': icao.upper(), 'total': len(charts), 'charts': charts})
                elif len(parts) == 6 and parts[:2] == ['api', 'v1'] and parts[2] == 'charts' and parts[4] == 'data':
                    icao, filename = parts[3], parts[5]
                    if not catalog.has_chart(icao, filename):
                        self._json(404, {'error': 'chart not found'})
                    else:
                        if filename not in catalog.chart_dims:
                            catalog.export_pdf(filename)  # dims only known after a render; also warms the export cache
                        self._json(200, catalog.get_chart_data(icao, filename))
                elif len(parts) == 6 and parts[:2] == ['api', 'v1'] and parts[2] == 'charts' and parts[4] == 'export':
                    icao, filename = parts[3], parts[5]
                    if not catalog.has_chart(icao, filename):
                        self._json(404, {'error': 'chart not found'})
                    else:
                        pdf_bytes = catalog.export_pdf(filename)
                        if pdf_bytes is None:
                            self._json(502, {'error': 'failed to render chart'})
                        else:
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/pdf')
                            self.send_header('Content-Length', str(len(pdf_bytes)))
                            self.end_headers()
                            self.wfile.write(pdf_bytes)
                else:
                    self._json(404, {'error': 'not found'})
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000
                print(f"{self.command} {self.path} -> {elapsed_ms:.1f}ms", file=sys.stderr)

    return Handler


class DualStackServer(ThreadingHTTPServer):
    """Binds one IPv6 socket that also accepts IPv4 (like Go's net.Listen does
    for "0.0.0.0"). Without this, Python's HTTPServer is IPv4-only, so on
    Windows a client resolving "localhost" to ::1 first stalls for ~10s
    waiting on the IPv6 connection before falling back to IPv4."""
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        with contextlib.suppress(Exception):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(host: str, port: int, handler):
    if host in ('0.0.0.0', '', '::'):
        return DualStackServer(('::', port), handler)
    return ThreadingHTTPServer((host, port), handler)


def hide_console() -> None:
    """Hide the console window this process is attached to (Windows only)."""
    if sys.platform != 'win32':
        return
    import ctypes
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        SW_HIDE = 0
        ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)


def main() -> int:
    parser = argparse.ArgumentParser(description="JeppQuery API server")
    parser.add_argument('-port', '--port', type=int, default=int(os.environ.get('PORT', 8080)))
    parser.add_argument('-host', '--host', default=os.environ.get('HOST', '0.0.0.0'))
    parser.add_argument('-charts', '--charts-dbf', default=os.environ.get('CHARTS_DBF', DEFAULT_CHARTS_DBF))
    parser.add_argument('-vfr', '--vfr-dbf', default=os.environ.get('VFR_DBF', DEFAULT_VFR_DBF))
    parser.add_argument('-types', '--types-dbf', default=os.environ.get('TYPES_DBF', DEFAULT_TYPES_DBF))
    parser.add_argument('-airports', '--airports-dbf', default=os.environ.get('AIRPORTS_DBF', DEFAULT_AIRPORTS_DBF))
    parser.add_argument('-tcls', '--tcl-dir', default=os.environ.get('TCL_DIR', DEFAULT_TCL_DIR))
    parser.add_argument(
        '-hidden', '--hidden', action='store_true',
        help="Hide the console window and log to a file instead of stdout",
    )
    parser.add_argument(
        '-log', '--log-file', default=os.environ.get('LOG_FILE', os.path.join(REPO_ROOT, 'jeppquery.log')),
        help="Log file to use when running hidden (default: jeppquery.log next to the repo root)",
    )
    args = parser.parse_args()

    if args.hidden:
        log_fd = open(args.log_file, 'a', buffering=1, encoding='utf-8')
        sys.stdout = log_fd
        sys.stderr = log_fd
        hide_console()

    print(f"Charts DBF: {args.charts_dbf}")
    print(f"VFR Charts DBF: {args.vfr_dbf}")
    print(f"Types DBF: {args.types_dbf}")
    print(f"TCL Directory: {args.tcl_dir}")
    print("Indexing charts by ICAO...")
    t0 = time.monotonic()
    catalog = ChartCatalog(args.charts_dbf, args.vfr_dbf, args.types_dbf, args.tcl_dir)
    elapsed = time.monotonic() - t0
    total_charts = sum(len(v) for v in catalog.charts_by_icao.values())
    print(f"Indexed {total_charts} charts across {len(catalog.charts_by_icao)} airports in {elapsed:.2f}s")

    server = make_server(args.host, args.port, make_handler(catalog))
    print(f"Starting JeppQuery API server on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
