# JeppQuery

Local API + web viewer for your installed Jeppesen terminal charts. Runs on your own machine, reads the Jeppesen data you already have.

Rebuild of [StarNumber12046/Marinvent](https://github.com/StarNumber12046/Marinvent) (DMCA'd, repo gone) off the leftover exes — the DBF/Charts.bin format reverse-engineering is their work. This version fixes it being unusably slow (17s+ per chart list), adds georeferencing, live aircraft position (MSFS/X-Plane), and a full web viewer.

## Requirements

- Windows
- Python 3
- Jeppesen data installed (`C:\ProgramData\Jeppesen\Common\TerminalCharts\`: charts.dbf, ctypes.dbf, vfrchrts.dbf, Charts.bin, fonts)
- `pip install Python-SimConnect` — only needed for the MSFS position feed (X-Plane and everything else is stdlib-only)

## Setup

```
python tools\tcl_extract.py -x C:\ProgramData\Jeppesen\Common\TerminalCharts\Charts.bin
python server\jeppquery_server.py
```

First command extracts every chart into `TCLs\` (run once, or again after a Jeppesen data update). Second starts the server on `0.0.0.0:8080` — open `http://localhost:8080` for the web viewer, or point BetterJepp at it.

`bin\jeppquery-server.exe` is a PyInstaller build of the same server for running without Python installed, but it's built manually and there's no CI for it — treat it as possibly stale and prefer running from source above until it's rebuilt.

## Options

| Flag | Effect |
|---|---|
| `-port <n>` | change port (default 8080) |
| `-host <addr>` | change bind address (default 0.0.0.0) |
| `-hidden` | no console window, logs to `jeppquery.log` |
| `-log <path>` | change log file location |
| `-no-msfs` / `-no-xplane` | disable a position feed |
| `-charts` / `-vfr` / `-types` / `-airports` / `-tcls` | override default data paths |

## Georeferencing

Aircraft-on-chart positioning uses `bin\georef_tool.exe`, which needs `mrvtcl.dll`, `mrvdrv.dll`, `zlib.dll` next to it in `bin\` (copied from a JeppView for Windows install — the coordinate math lives in Jeppesen's own DLL, not in Marinvent). Missing DLLs just means every chart reports as ungeoreferenced, not an error.

Not every chart carries georeferencing data — this is upstream Jeppesen data, not a bug. Ground/parking-stand charts are typically ungeoreferenced; SIDs, STARs, and approach plates usually are.

Live position comes from `tools/sim_feed.py` (MSFS SimConnect or X-Plane UDP, auto-detected).

## Troubleshooting

- **"Failed to load charts"** — server isn't running or client is pointed at the wrong port.
- **Chart won't render** — first render takes ~1s (actual PDF generation), cached after that. If it never finishes, confirm `TCLs\` has that chart's file.
- **Startup says a DBF is missing** — your Jeppesen install isn't at the default path; point at it with `-charts`/`-vfr`/`-types`.
- **Stale chart data after a Jeppesen update** — re-run the extract step, then delete `cache\` (rendered PDFs are cached by filename and won't auto-refresh).

## Not implemented

- Airport info lookups (`airports.dbf` not wired up)
