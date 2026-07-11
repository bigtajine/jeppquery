# JeppQuery

Jeppesen chart API, feeds BetterJepp. Rebuild of [StarNumber12046/Marinvent](https://github.com/StarNumber12046/Marinvent) (DMCA'd, gone) off the leftover exes — all format/DBF work is theirs.

Fix: original scanned all 82k charts.dbf rows per request (~17s, client timeout). Now indexed by ICAO at startup, O(1).

`server/jeppquery_server.py` — server. `tools/` — dbf.py/checksum.py/pdf_fixup (Marinvent's). `bin/tcl2emf.exe` — TCL→PDF (Marinvent's, Windows only). `legacy/` — original exes + last readme.

Run: `python tools\tcl_extract.py -x <Charts.bin>` then `python server\jeppquery_server.py` (or `bin\jeppquery-server.exe`). `-port`/`PORT`, `-host`/`HOST`, `-hidden` (no console, logs to `jeppquery.log`).

Endpoints: `/health`, `/api/v1/chart-types`, `/api/v1/charts/{icao}` (`?type=` `?search=`), `/api/v1/charts/{icao}/export/{filename}` (cached PDF render).

Build exe: `pyinstaller --onefile --console --name jeppquery-server --distpath bin --paths tools server\jeppquery_server.py`. Needed `sys.executable` not `__file__` for path resolution in frozen mode (onefile extracts to temp dir).

TODO: georef_tool.exe not ported, airports.dbf not wired up.
