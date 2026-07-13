# JeppQuery

Local API that serves your Jeppesen terminal charts to chart-viewer apps like BetterJepp. Runs on your own machine, reads the Jeppesen data you already have installed.

Rebuild of [StarNumber12046/Marinvent](https://github.com/StarNumber12046/Marinvent) (DMCA'd, repo gone) off the leftover exes — all the DBF/charts.bin format reverse-engineering is their work, not mine. This version fixes it being unusably slow (17s+ to load a chart list) and adds a couple missing features.

## Requirements

- Windows
- Jeppesen data installed (the usual `C:\ProgramData\Jeppesen\Common\TerminalCharts\` files: charts.dbf, ctypes.dbf, vfrchrts.dbf, Charts.bin, plus fonts)
- Nothing else — `bin\jeppquery-server.exe` is standalone, no Python/install needed

## First time setup

1. Extract your chart data (only needed once, or again after Jeppesen updates):
   ```
   python tools\tcl_extract.py -x C:\ProgramData\Jeppesen\Common\TerminalCharts\Charts.bin
   ```
   Run this from the repo root — it pulls every chart out of Charts.bin into `TCLs\`.

2. Start the server:
   ```
   bin\jeppquery-server.exe
   ```
   That's it. It listens on `0.0.0.0:8080` by default. Point BetterJepp (or whatever) at `http://localhost:8080`.

## Changing the port

```
bin\jeppquery-server.exe -port 9000
```
or set it once as an environment variable instead: `set PORT=9000`.

Same deal for the bind address with `-host` / `HOST` if you need something other than `0.0.0.0`.

## Running with no console window

```
bin\jeppquery-server.exe -hidden
```
Hides the window (good for autostart / running in the background). Logs go to `jeppquery.log` in the repo root instead of the console. Change the log location with `-log <path>`.

## Updating chart data

Whenever Jeppesen data updates, re-run the extraction step from setup — it's safe to run again, just overwrites `TCLs\`. If chart *rendering* looks stale afterward, delete the `cache\` folder too (it caches rendered PDFs by filename).

## Troubleshooting

- **"Failed to load charts" in the client** — server probably isn't running, or is pointed at the wrong port. Check the console output / log file for errors.
- **A chart won't render / times out** — first render of any chart takes ~1 second (it's actually drawing the PDF), every render after that is instant from cache. If it never finishes, check that `TCLs\` actually has that chart's file.
- **Startup says a DBF file is missing** — your Jeppesen paths aren't the defaults. Point at them with `-charts`, `-vfr`, `-types` flags (see `bin\jeppquery-server.exe -h`).

## Georeferencing (aircraft position on charts)

Wired up via `bin\georef_tool.exe`, which needs `mrvtcl.dll`, `mrvdrv.dll`, and `zlib.dll` sitting next to it in `bin\` — these came from a JeppView for Windows install (`C:\Program Files (x86)\Jeppesen\JeppView for Windows\`), not from Marinvent, since the actual coordinate-conversion math turned out to live in Jeppesen's own DLL rather than anything Marinvent reimplemented. If those three DLLs are missing, `/geo/*` endpoints just report every chart as ungeoreferenced instead of erroring.

Live aircraft position comes from `tools/sim_feed.py` — MSFS (SimConnect) or X-Plane (UDP datarefs), both auto-detected, no config needed beyond having the sim running. `-no-msfs`/`-no-xplane` to disable either.

## Not implemented yet

- Airport info lookups (`airports.dbf` isn't wired up)
