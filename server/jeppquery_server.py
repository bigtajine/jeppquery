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
from sim_feed import PositionStore, start_msfs_feed, start_xplane_feed  # noqa: E402

FILENAME_RE = re.compile(r'^[A-Za-z0-9_]+$')
CHART_DIMS_RE = re.compile(r'Chart:\s*(\d+)\s*x\s*(\d+)')
NATURAL_SORT_RE = re.compile(r'(\d+)')

CATEGORY_RANK = {'AIRPORT': 0, 'DEPARTURE': 1, 'ARRIVAL': 2, 'APPROACH': 3, 'TEXT': 4}


def natural_sort_key(s: str):
    # "10-8A" -> [10, '-', 8, 'a'] so 10-4 sorts before 10-8 before 10-8A,
    # instead of comparing INDEX_NO as a plain string.
    parts = NATURAL_SORT_RE.split(s)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def chart_sort_key(record: dict):
    rank = CATEGORY_RANK.get(record['category'].upper(), 5)
    return (rank, natural_sort_key(record.get('index_no') or ''), record['proc_id'])

INDEX_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>JeppQuery</title>
<style>
  *{box-sizing:border-box}
  :root{
    --bg:#131722; --panel:#171c29; --border:#232939; --text:#e6e9f0; --muted:#8b93a7;
    --accent:#4f6bfb;
    --Airport:#4f6bfb; --Departure:#f2a33e; --Arrival:#3fcf7f; --Approach:#ef5b6b; --Reference:#b473f2;
  }
  body{background:var(--bg);color:var(--text);font-family:-apple-system,"Segoe UI",Roboto,Inter,sans-serif;margin:0;display:flex;height:100vh;font-size:14px}
  /* button/input/select don't inherit font-family by default in any browser
     -- without this every form control in the toolbar/popup falls back to
     the OS UI font instead of matching the rest of the app. */
  button,input,select,textarea{font-family:inherit}
  #sidebar{width:340px;border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}
  #sidebar.collapsed{width:44px}
  #sidebar.collapsed #tabs,#sidebar.collapsed #list{display:none}
  #sidebar.collapsed #search{padding:8px 0;justify-content:center}
  #sidebar.collapsed #icao{display:none}
  #panel-toggle{width:28px;height:28px;min-width:28px;min-height:28px;flex-shrink:0;padding:0;background:transparent;border:none;color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;border-radius:6px}
  #panel-toggle:hover{background:var(--panel);color:var(--text)}
  #panel-toggle svg{display:block;flex-shrink:0}
  #search{display:flex;align-items:center;gap:8px;padding:10px 10px 10px 14px;flex-shrink:0}
  input{width:100%;padding:9px 10px;background:var(--panel);border:1px solid var(--border);border-radius:8px;color:var(--text);font:inherit}
  #icao{flex:1;min-width:0;width:auto}
  input:focus{outline:none;border-color:var(--accent)}
  input:-webkit-autofill{-webkit-box-shadow:0 0 0 1000px var(--panel) inset;-webkit-text-fill-color:var(--text);caret-color:var(--text)}
  #tabs{display:flex;gap:14px;padding:2px 14px 12px;font-weight:600;font-size:13px;flex-wrap:wrap}
  #tabs span{color:var(--muted);cursor:pointer;padding-bottom:4px;border-bottom:2px solid transparent}
  #tabs span.active{color:var(--text);border-color:var(--accent)}
  #tabs span[data-tab=Departure].active{color:var(--Departure);border-color:var(--Departure)}
  #tabs span[data-tab=Arrival].active{color:var(--Arrival);border-color:var(--Arrival)}
  #tabs span[data-tab=Approach].active{color:var(--Approach);border-color:var(--Approach)}
  #tabs span[data-tab=Reference].active{color:var(--Reference);border-color:var(--Reference)}
  #list{flex:1;overflow-y:auto;padding:0 8px 8px}
  .chart{display:flex;gap:10px;align-items:center;padding:9px 8px;border-radius:8px;cursor:pointer}
  .chart:hover{background:var(--panel)}
  .chart.active{background:#1d2436}
  .bar{width:3px;align-self:stretch;border-radius:2px;background:var(--Airport)}
  .bar.Departure{background:var(--Departure)} .bar.Arrival{background:var(--Arrival)}
  .bar.Approach{background:var(--Approach)} .bar.Reference{background:var(--Reference)}
  .name{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .type{color:var(--muted);font-size:11.5px;margin-top:1px}
  #viewer{flex:1;display:flex;flex-direction:column;background:#0d1017;min-width:0}
  #zoombar{display:flex;align-items:center;justify-content:flex-end;gap:4px;padding:6px 12px;border-bottom:1px solid var(--border)}
  #zoombar button{background:transparent;border:1px solid transparent;color:var(--text);width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px;line-height:1;display:flex;align-items:center;justify-content:center}
  #zoombar button:hover{background:var(--panel);border-color:var(--border)}
  #zoombar button.active{background:var(--panel);border-color:var(--accent);color:var(--accent)}
  #zoombar button:disabled{opacity:.35;cursor:default}
  #zoombar button:disabled:hover{background:transparent;border-color:transparent}
  #zoombar .sep{width:1px;height:20px;background:var(--border);margin:0 4px}
  #zoombar .zoom-label{color:var(--muted);font-size:11.5px;min-width:38px;text-align:center;background:var(--panel);border-radius:5px;padding:4px 2px}
  #draw-popup-wrap{position:relative}
  #draw-popup{position:absolute;top:34px;right:0;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px;box-shadow:0 8px 24px rgba(0,0,0,.4);z-index:50;width:168px}
  #draw-popup .label{font-size:9.5px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}
  #draw-popup .colors{display:flex;gap:6px;margin-bottom:10px}
  #draw-popup .color-swatch{width:20px;height:20px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0}
  #draw-popup .color-swatch.active{border-color:var(--text)}
  #draw-popup input[type=range]{width:100%;accent-color:var(--accent)}
  #draw-popup .thickness-val{text-align:center;color:var(--muted);font-size:10px;margin-top:2px}
  #scroller{flex:1;overflow:auto;display:flex;padding:24px;cursor:grab;position:relative}
  #scroller.dragging{cursor:grabbing}
  #page-wrap{position:relative;margin:auto}
  #page-wrap.dark canvas#page-canvas{filter:invert(1) hue-rotate(180deg)}
  #page-wrap.dark canvas#draw-canvas{filter:invert(1)}
  canvas{display:block;background:#fff;visibility:hidden}
  canvas.ready{visibility:visible}
  #draw-canvas{position:absolute;inset:0;background:transparent!important;visibility:visible!important}
  #loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;color:var(--muted);font-size:13px}
  #loading::before{content:'';display:block;width:22px;height:22px;margin:0 0 10px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  #empty{margin:auto;color:#3a4152;font-size:13px}
  .position-arrow{position:absolute;pointer-events:none;z-index:20}
  .position-arrow .ring{position:absolute;border-radius:50%;left:50%;top:50%;transform:translate(-50%,-50%);animation:pulse-ring 2s ease-in-out infinite}
  @keyframes pulse-ring{
    0%{opacity:.6;transform:translate(-50%,-50%) scale(.7)}
    50%{opacity:.2;transform:translate(-50%,-50%) scale(1)}
    100%{opacity:.6;transform:translate(-50%,-50%) scale(.7)}
  }
</style></head>
<body>
<div id="sidebar">
  <div id="search">
    <input id="icao" placeholder="Search ICAO..." maxlength="4" autocomplete="off" autofocus>
    <button id="panel-toggle" title="Collapse Panel"></button>
  </div>
  <div id="tabs"></div>
  <div id="list"></div>
</div>
<div id="viewer"><div id="empty">Select a chart</div></div>
<canvas id="page-canvas-buffer" style="position:fixed;left:-99999px;top:0"></canvas>
<script type="module">
import * as pdfjsLib from '/vendor/pdf.min.mjs';
pdfjsLib.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.mjs';

const icaoInput = document.getElementById('icao');
const list = document.getElementById('list');
const viewer = document.getElementById('viewer');
const tabsEl = document.getElementById('tabs');

// --- Sidebar collapse/expand, ported from BetterJepp's panel toggle
// (PanelLeft / PanelLeftClose + panelCollapsed state).
const PANEL_LEFT_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/></svg>';
const PANEL_LEFT_CLOSE_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/><path d="m16 15-3-3 3-3"/></svg>';
const sidebarEl = document.getElementById('sidebar');
const panelToggle = document.getElementById('panel-toggle');
let panelCollapsed = false;

function updatePanelToggle() {
  sidebarEl.classList.toggle('collapsed', panelCollapsed);
  panelToggle.innerHTML = panelCollapsed ? PANEL_LEFT_ICON : PANEL_LEFT_CLOSE_ICON;
  panelToggle.title = panelCollapsed ? 'Expand Panel' : 'Collapse Panel';
}
panelToggle.addEventListener('click', () => {
  panelCollapsed = !panelCollapsed;
  updatePanelToggle();
});
updatePanelToggle();

// --- PDF rendering: pdf.js onto a <canvas> instead of an <iframe>. An
// iframe embeds the browser's own opaque PDF viewer, which has its own
// internal zoom/scroll the parent page can't see -- so a percent-positioned
// overlay drifts out of alignment the moment the user zooms inside it.
// Rendering to a canvas we control means "percent of the page" is always
// correct, at any zoom, because we're the one deciding the page's pixel size.
// Same model as BetterJepp's real ChartViewer.tsx: fitScale (computed to
// fit the container, recalculated on resize) times pdfZoom (the user's
// zoom-in/out on top of that, default 1.0 = "fit"). Wheel zooms with no
// modifier key, drag-to-pan via scrollLeft/scrollTop -- ported directly
// from their handleWheel/handleMouseDown/handleMouseMove.
let pdfDoc = null, pdfPage = null, fitScale = 1.0, pdfZoom = 1.0;
const MIN_ZOOM = 0.25, MAX_ZOOM = 4.0;
let isDragging = false, dragStart = null;

function updateZoomLabel() {
  const label = document.getElementById('zoom-label');
  if (label) label.textContent = `${Math.round(pdfZoom * 100)}%`;
}

async function recalcFitScale() {
  if (!pdfPage) return;
  const scroller = document.getElementById('scroller');
  if (!scroller) return;
  const naturalViewport = pdfPage.getViewport({ scale: 1 });
  const containerW = scroller.clientWidth - 48;
  const containerH = scroller.clientHeight - 48;
  const pageW = pdfRotation % 180 === 0 ? naturalViewport.width : naturalViewport.height;
  const pageH = pdfRotation % 180 === 0 ? naturalViewport.height : naturalViewport.width;
  if (containerW > 0 && containerH > 0) {
    fitScale = Math.min(containerW / pageW, containerH / pageH, 3.0) * 0.95;
    await renderPage();
  }
}

// pdf.js's RenderTask.cancel() turned out to leave the PDFPageProxy's
// internal render state wedged -- every render() call after a cancelled one
// hung forever, even completely fresh ones with nothing else in flight.
// Simpler and it turns out more reliable: never run two renders
// concurrently at all. If a render is requested while one is already
// running, just remember to run once more with the latest zoom/fitScale
// when it finishes, instead of cancelling anything.
let isRendering = false, renderPending = false;

async function renderPage() {
  if (!pdfPage) return;
  if (isRendering) {
    renderPending = true;
    return;
  }
  isRendering = true;
  const dpr = window.devicePixelRatio || 1;
  const cssScale = fitScale * pdfZoom;
  const cssViewport = pdfPage.getViewport({ scale: cssScale, rotation: pdfRotation });
  // Rendering at fitScale * pdfZoom * devicePixelRatio with no cap means the
  // render surface's pixel AREA grows with the square of zoom -- on a HiDPI
  // display (dpr 2-3) combined with high pdfZoom this balloons into a huge
  // canvas that pdf.js takes noticeably longer to rasterize each time,
  // which is exactly what "zoom starts lagging" looks like (each further
  // zoom step is slower than the last). Cap the actual rendered pixel
  // dimensions and let CSS upscale the rest of the way if needed -- past a
  // few thousand px there's no more real detail in the source PDF to gain
  // from rendering higher-res anyway.
  const MAX_RENDER_DIM = 4096;
  const renderScaleCap = Math.min(dpr, MAX_RENDER_DIM / Math.max(cssViewport.width, cssViewport.height, 1));
  const viewport = pdfPage.getViewport({ scale: cssScale * renderScaleCap, rotation: pdfRotation });
  // Render into an offscreen buffer canvas, not the one on screen. Setting
  // .width/.height on a canvas clears it instantly, and pdf.js then takes a
  // moment to repaint -- on the visible canvas that shows as a white flash
  // every time you zoom. Rendering off-screen and copying the finished
  // image over in one synchronous drawImage() call means the visible
  // canvas only ever shows a complete frame, never a blank intermediate one
  // (the browser can't paint mid-script, so there's no frame to flash).
  const buffer = document.getElementById('page-canvas-buffer');
  buffer.width = viewport.width;
  buffer.height = viewport.height;
  await pdfPage.render({ canvasContext: buffer.getContext('2d'), viewport }).promise;
  isRendering = false;
  if (renderPending) {
    renderPending = false;
    return renderPage();
  }
  const canvas = document.getElementById('page-canvas');
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  canvas.getContext('2d').drawImage(buffer, 0, 0);
  canvas.style.width = `${cssViewport.width}px`;
  canvas.style.height = `${cssViewport.height}px`;
  const pageWrap = document.getElementById('page-wrap');
  pageWrap.style.width = canvas.style.width;
  pageWrap.style.height = canvas.style.height;
  // Mirror the exact same CSS pixel size onto the drawing overlay directly,
  // instead of trusting `inset:0` to stretch it to match -- that's what was
  // causing drawing to only work in a small corner (the overlay's rendered
  // box wasn't actually filling page-wrap the way it looked like it should).
  const drawCanvas = document.getElementById('draw-canvas');
  if (drawCanvas) {
    drawCanvas.style.width = canvas.style.width;
    drawCanvas.style.height = canvas.style.height;
  }
  canvas.classList.add('ready');
  const loading = document.getElementById('loading');
  if (loading) loading.remove();
  redrawStrokes();
  renderArrow();
}

function setZoom(z) {
  pdfZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
  updateZoomLabel();
  renderPage();
}

// Same filter as the shipped BetterJepp build's dark mode: invert(1) alone
// flips every hue (blues become oranges etc.), hue-rotate(180deg) on top of
// that brings hues back close to their original appearance while keeping
// the light/dark inversion. The draw layer gets plain invert(1) (no
// hue-rotate), same split BetterJepp uses.
let pdfDarkMode = false;
let pdfRotation = 0;
let isPinned = false;

function setDarkMode(on) {
  pdfDarkMode = on;
  const pageWrap = document.getElementById('page-wrap');
  if (pageWrap) pageWrap.classList.toggle('dark', pdfDarkMode);
  const btn = document.getElementById('dark-toggle');
  if (btn) btn.innerHTML = pdfDarkMode ? ICONS.sun : ICONS.moon;
}

// --- Freehand drawing overlay, ported from BetterJepp's ChartDrawingCanvas
// + useDrawingStore. Strokes are stored as points normalized to 0..1 of the
// canvas size, so they stay aligned with the chart at any zoom level without
// needing to be rescaled -- same trick the position arrow overlay uses.
const PEN_COLORS = ['#ef4444', '#3b82f6', '#22c55e', '#f59e0b', '#ffffff'];
let activeTool = 'none'; // 'none' | 'pen' | 'eraser'
let penColor = PEN_COLORS[0];
let penWidth = 4;
let chartStrokes = {}; // filename -> [{points, color, width, tool}]
let currentStroke = null;
let isDrawingStroke = false;

function drawCanvasKey() {
  return currentFilename;
}

function syncDrawCanvasSize() {
  const canvas = document.getElementById('draw-canvas');
  if (!canvas) return;
  const w = Math.max(1, Math.round(canvas.offsetWidth));
  const h = Math.max(1, Math.round(canvas.offsetHeight));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
}

function redrawStrokes() {
  const canvas = document.getElementById('draw-canvas');
  if (!canvas) return;
  syncDrawCanvasSize();
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const strokes = chartStrokes[drawCanvasKey()] || [];
  const allStrokes = currentStroke ? [...strokes, currentStroke] : strokes;
  for (const stroke of allStrokes) {
    if (stroke.points.length < 2) continue;
    ctx.beginPath();
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    if (stroke.tool === 'eraser') {
      ctx.globalCompositeOperation = 'destination-out';
      ctx.lineWidth = stroke.width * 4;
    } else {
      ctx.globalCompositeOperation = 'source-over';
      ctx.strokeStyle = stroke.color;
      ctx.lineWidth = stroke.width;
    }
    ctx.moveTo(stroke.points[0].x * canvas.width, stroke.points[0].y * canvas.height);
    for (let i = 1; i < stroke.points.length; i++) {
      ctx.lineTo(stroke.points[i].x * canvas.width, stroke.points[i].y * canvas.height);
    }
    ctx.stroke();
  }
  ctx.globalCompositeOperation = 'source-over';
}

function drawPos(e) {
  const canvas = document.getElementById('draw-canvas');
  const rect = canvas.getBoundingClientRect();
  return { x: (e.clientX - rect.left) / rect.width, y: (e.clientY - rect.top) / rect.height };
}

function setActiveTool(tool) {
  activeTool = tool;
  const canvas = document.getElementById('draw-canvas');
  if (canvas) canvas.style.pointerEvents = tool !== 'none' ? 'auto' : 'none';
  document.getElementById('draw-pen')?.classList.toggle('active', tool === 'pen');
  document.getElementById('draw-eraser')?.classList.toggle('active', tool === 'eraser');
}

function updateTrashVisibility() {
  const trash = document.getElementById('draw-trash');
  if (trash) trash.style.display = (chartStrokes[drawCanvasKey()] || []).length ? '' : 'none';
}

document.addEventListener('click', e => {
  // .closest() here, not e.target.id === -- these buttons are mostly
  // filled by their SVG icon, so most clicks land on the <svg>/<path>
  // child, not the <button> itself. A plain id check only matched clicks
  // on the sliver of the button not covered by the icon, which is exactly
  // what "finnicky, inconsistent" zoom buttons looks like.
  if (e.target.closest('#zoom-in')) setZoom(pdfZoom + 0.1);
  if (e.target.closest('#zoom-out')) setZoom(pdfZoom - 0.1);
  if (e.target.closest('#zoom-reset')) setZoom(1.0);
  if (e.target.closest('#dark-toggle')) setDarkMode(!pdfDarkMode);
  if (e.target.closest('#draw-pen')) {
    const popup = document.getElementById('draw-popup');
    if (activeTool === 'pen') {
      setActiveTool('none');
      if (popup) popup.remove();
    } else {
      setActiveTool('pen');
    }
  }
  if (e.target.closest('#draw-eraser')) setActiveTool(activeTool === 'eraser' ? 'none' : 'eraser');
  if (e.target.closest('#draw-trash')) {
    delete chartStrokes[drawCanvasKey()];
    redrawStrokes();
    updateTrashVisibility();
  }
  if (e.target.closest('#rotate-btn')) {
    pdfRotation = (pdfRotation + 90) % 360;
    recalcFitScale();
  }
  if (e.target.closest('#pin-btn')) {
    isPinned = !isPinned;
    e.target.closest('#pin-btn').classList.toggle('active', isPinned);
  }
  if (e.target.closest('#close-btn')) {
    viewer.innerHTML = '<div id="empty">Select a chart</div>';
    currentFilename = null;
    pdfDoc = null;
    pdfPage = null;
    document.querySelectorAll('.chart.active').forEach(x => x.classList.remove('active'));
  }
  if (e.target.classList.contains('color-swatch')) {
    penColor = e.target.dataset.color;
    document.querySelectorAll('.color-swatch').forEach(s => s.classList.toggle('active', s.dataset.color === penColor));
    document.getElementById('draw-pen').style.color = penColor;
  }
  const popup = document.getElementById('draw-popup');
  const penBtn = document.getElementById('draw-pen');
  if (popup && !popup.contains(e.target) && e.target !== penBtn && !penBtn?.contains(e.target)) {
    popup.remove();
  }
});

document.addEventListener('contextmenu', e => {
  if (!e.target.closest('#draw-pen')) return;
  e.preventDefault();
  toggleDrawPopup();
});

document.addEventListener('input', e => {
  if (e.target.id === 'draw-thickness') {
    penWidth = Number(e.target.value);
    const val = document.getElementById('draw-thickness-val');
    if (val) val.textContent = `${penWidth % 1 === 0 ? penWidth.toFixed(0) : penWidth.toFixed(1)} px`;
  }
});

function toggleDrawPopup() {
  const existing = document.getElementById('draw-popup');
  if (existing) { existing.remove(); return; }
  const wrap = document.getElementById('draw-popup-wrap');
  if (!wrap) return;
  const popup = document.createElement('div');
  popup.id = 'draw-popup';
  popup.innerHTML = `
    <div class="label">Color</div>
    <div class="colors">${PEN_COLORS.map(c => `<button type="button" class="color-swatch${c === penColor ? ' active' : ''}" data-color="${c}" style="background:${c}"></button>`).join('')}</div>
    <div class="label">Thickness</div>
    <input type="range" id="draw-thickness" min="0.5" max="8" step="0.5" value="${penWidth}">
    <div class="thickness-val" id="draw-thickness-val">${penWidth % 1 === 0 ? penWidth.toFixed(0) : penWidth.toFixed(1)} px</div>`;
  wrap.appendChild(popup);
}

document.addEventListener('wheel', e => {
  const scroller = document.getElementById('scroller');
  if (!scroller || !scroller.contains(e.target)) return;
  e.preventDefault();
  setZoom(pdfZoom + (e.deltaY > 0 ? -0.1 : 0.1));
}, { passive: false });

document.addEventListener('mousedown', e => {
  if (activeTool !== 'none' && e.target.id === 'draw-canvas') {
    syncDrawCanvasSize();
    isDrawingStroke = true;
    currentStroke = { points: [drawPos(e)], color: penColor, width: penWidth, tool: activeTool };
    return;
  }
  const scroller = document.getElementById('scroller');
  if (!scroller || !scroller.contains(e.target)) return;
  isDragging = true;
  scroller.classList.add('dragging');
  dragStart = { x: e.clientX, y: e.clientY, scrollLeft: scroller.scrollLeft, scrollTop: scroller.scrollTop };
});

document.addEventListener('mousemove', e => {
  if (isDrawingStroke && currentStroke) {
    currentStroke.points.push(drawPos(e));
    redrawStrokes();
    return;
  }
  if (!isDragging || !dragStart) return;
  const scroller = document.getElementById('scroller');
  if (!scroller) return;
  scroller.scrollLeft = dragStart.scrollLeft - (e.clientX - dragStart.x);
  scroller.scrollTop = dragStart.scrollTop - (e.clientY - dragStart.y);
});

function finishStroke() {
  if (!isDrawingStroke) return;
  isDrawingStroke = false;
  if (currentStroke && currentStroke.points.length >= 2) {
    const key = drawCanvasKey();
    (chartStrokes[key] = chartStrokes[key] || []).push(currentStroke);
  }
  currentStroke = null;
  redrawStrokes();
  updateTrashVisibility();
}

document.addEventListener('mouseup', () => {
  finishStroke();
  isDragging = false;
  dragStart = null;
  const scroller = document.getElementById('scroller');
  if (scroller) scroller.classList.remove('dragging');
});

// ResizeObserver fires once immediately on observe() even with no real
// resize, which raced with selectChart's own initial recalcFitScale() call
// and cancelled each render before it could ever finish -- debounce so
// only a genuine, settled resize triggers a re-fit.
let resizeDebounce = null;
new ResizeObserver(() => {
  clearTimeout(resizeDebounce);
  resizeDebounce = setTimeout(recalcFitScale, 150);
}).observe(document.getElementById('viewer'));

// --- Aircraft position overlay, ported from BetterJepp's ChartViewer.tsx
// (PositionArrow component + coord2pixel polling). Position comes from the
// server's /api/v1/aircraft/position, fed by a background MSFS SimConnect
// or X-Plane UDP thread (see tools/sim_feed.py). window.setAircraftPosition
// is still exposed for manual testing when no sim is running.
let chartGeoStatus = null;
let aircraftPosition = null;
let pixelPosition = null;
let lastCoord2Pixel = 0;
let coord2PixelTimer = null;

window.setAircraftPosition = (pos) => {
  aircraftPosition = pos;
  scheduleCoord2Pixel();
};

setInterval(async () => {
  try {
    const res = await fetch('/api/v1/aircraft/position');
    const data = await res.json();
    aircraftPosition = (data && data.lat != null) ? { lat: data.lat, lon: data.lon, heading: data.heading } : null;
    scheduleCoord2Pixel();
  } catch (e) {
    // sim feed unreachable this tick, leave last known position as-is
  }
}, 1000);

// Exact same icon/effects as the shipped BetterJepp build's PositionArrow
// (extracted from its own-ship marker: orange nav arrowhead SVG + double
// pulse-ring halo), not the plain blue triangle from the older git checkout.
const OWN_SHIP_MARKER_RGB = '234, 88, 12';
const AIRCRAFT_POSITION_MARKER_PX = 28;
const AIRCRAFT_POSITION_MARKER_PULSE_PX = 44;
const AIRCRAFT_POSITION_MARKER_URL = "data:image/svg+xml,%3csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20100%20100'%20width='100'%20height='100'%3e%3c!--%20Solid%20nav-style%20arrowhead%20(tip%20up);%20rotation%20applied%20in%20ChartViewer%20--%3e%3cpath%20fill='%23EA580C'%20d='M50%204%20L94%2086%20Q50%2058%206%2086%20Z'%20/%3e%3c/svg%3e";

function positionArrowHtml(percentX, percentY, heading, rotation) {
  const adjustedHeading = heading - rotation;
  const pulsePx = AIRCRAFT_POSITION_MARKER_PULSE_PX;
  const iconPx = AIRCRAFT_POSITION_MARKER_PX;
  return `<div class="position-arrow" style="left:${percentX * 100}%;top:${percentY * 100}%;transform:translate(-50%,-50%)">
    <div class="ring" style="width:${pulsePx}px;height:${pulsePx}px;border:2px solid rgba(${OWN_SHIP_MARKER_RGB}, 0.55)"></div>
    <div class="ring" style="width:${pulsePx}px;height:${pulsePx}px;background:rgba(${OWN_SHIP_MARKER_RGB}, 0.1)"></div>
    <img src="${AIRCRAFT_POSITION_MARKER_URL}" width="${iconPx}" height="${iconPx}" alt="" draggable="false"
      style="position:relative;display:block;pointer-events:none;user-select:none;max-width:${iconPx}px;max-height:${iconPx}px;transform:rotate(${adjustedHeading}deg);filter:drop-shadow(0 1px 3px rgba(0,0,0,0.45))">
  </div>`;
}

function renderArrow() {
  const arrowEl = document.getElementById('position-arrow-slot');
  if (!arrowEl) return;
  if (chartGeoStatus?.georef?.georeferenced && pixelPosition && aircraftPosition &&
      chartGeoStatus.width > 0 && chartGeoStatus.height > 0) {
    arrowEl.innerHTML = positionArrowHtml(
      pixelPosition.x / chartGeoStatus.width,
      pixelPosition.y / chartGeoStatus.height,
      aircraftPosition.heading ?? 0,
      0
    );
  } else {
    arrowEl.innerHTML = '';
  }
}

function scheduleCoord2Pixel() {
  if (!chartGeoStatus?.georef?.georeferenced || !aircraftPosition) {
    pixelPosition = null;
    renderArrow();
    return;
  }
  const THROTTLE_MS = 1000;
  const elapsed = Date.now() - lastCoord2Pixel;
  if (coord2PixelTimer) clearTimeout(coord2PixelTimer);
  coord2PixelTimer = setTimeout(fetchCoord2Pixel, Math.max(0, THROTTLE_MS - elapsed));
}

async function fetchCoord2Pixel() {
  lastCoord2Pixel = Date.now();
  try {
    const res = await fetch(`/api/v1/charts/${icao}/geo/coord2pixel/${currentFilename}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ latitude: aircraftPosition.lat, longitude: aircraftPosition.lon })
    });
    if (res.ok) {
      const data = await res.json();
      pixelPosition = data.error ? null : { x: data.x, y: data.y };
    } else {
      pixelPosition = null;
    }
  } catch (e) {
    pixelPosition = null;
  }
  renderArrow();
}
const TAB_ORDER = ['Airport', 'Departure', 'Arrival', 'Approach', 'Reference'];
let charts = [], icao = '', tab = 'All', currentFilename = null;

icaoInput.addEventListener('input', () => {
  const upper = icaoInput.value.toUpperCase();
  if (upper !== icaoInput.value) icaoInput.value = upper;
});
icaoInput.addEventListener('keydown', e => { if (e.key === 'Enter') load(icaoInput.value.trim()); });
tabsEl.addEventListener('click', e => {
  if (e.target.tagName !== 'SPAN') return;
  tab = e.target.dataset.tab;
  renderTabs();
  renderList();
});

function bucket(c) {
  const cat = (c.category || '').toUpperCase();
  if (cat === 'AIRPORT') return 'Airport';
  if (cat === 'DEPARTURE') return 'Departure';
  if (cat === 'ARRIVAL') return 'Arrival';
  if (cat === 'APPROACH') return 'Approach';
  if (cat === 'TEXT') return 'Reference';
  return null;
}

async function load(value) {
  if (!value) return;
  icao = value;
  list.innerHTML = '<div class="type" style="padding:8px">loading...</div>';
  try {
    const res = await fetch(`/api/v1/charts/${icao}`);
    const data = await res.json();
    charts = data.charts || [];
    tab = 'All';
    renderTabs();
    renderList();
  } catch (e) {
    list.innerHTML = '<div class="type" style="padding:8px">failed to load</div>';
  }
}

function renderTabs() {
  const present = TAB_ORDER.filter(t => charts.some(c => bucket(c) === t));
  const all = ['All', ...present];
  tabsEl.innerHTML = all.map(t =>
    `<span data-tab="${t}" class="${t === tab ? 'active' : ''}">${t}</span>`
  ).join('');
}

function renderList() {
  const filtered = tab === 'All' ? charts : charts.filter(c => bucket(c) === tab);
  list.innerHTML = '';
  if (!filtered.length) { list.innerHTML = '<div class="type" style="padding:8px">no charts</div>'; return; }
  filtered.forEach(c => {
    const b = bucket(c) || 'Airport';
    const div = document.createElement('div');
    div.className = 'chart';
    div.innerHTML = `<div class="bar ${b}"></div><div><div class="name">${c.proc_id}</div><div class="type">${c.type_name}</div></div>`;
    div.onclick = () => selectChart(c, div);
    list.appendChild(div);
  });
}

const ICONS = {
  pin: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/></svg>',
  rotate: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>',
  zoomOut: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/><path d="M8 11h6"/></svg>',
  zoomIn: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/><path d="M11 8v6"/><path d="M8 11h6"/></svg>',
  maximize: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>',
  pencil: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.986L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/></svg>',
  eraser: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 21H8a2 2 0 0 1-1.42-.587l-3.994-3.999a2 2 0 0 1 0-2.828l10-10a2 2 0 0 1 2.829 0l5.999 6a2 2 0 0 1 0 2.828L12.834 21"/><path d="m5.082 11.09 8.828 8.828"/></svg>',
  trash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  moon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>',
  sun: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>',
  close: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>',
};

async function selectChart(c, el) {
  document.querySelectorAll('.chart.active').forEach(x => x.classList.remove('active'));
  el.classList.add('active');
  currentFilename = c.filename;
  chartGeoStatus = null;
  pixelPosition = null;
  pdfDoc = null;
  pdfPage = null;
  fitScale = 1.0;
  pdfZoom = 1.0;
  pdfRotation = 0;
  isPinned = false;
  setActiveTool('none');

  viewer.innerHTML = `<div id="zoombar">
      <button id="pin-btn" title="Pin chart">${ICONS.pin}</button>
      <button id="rotate-btn" title="Rotate 90&#176;">${ICONS.rotate}</button>
      <div class="sep"></div>
      <button id="zoom-out" title="Zoom out">${ICONS.zoomOut}</button>
      <span id="zoom-label" class="zoom-label">100%</span>
      <button id="zoom-in" title="Zoom in">${ICONS.zoomIn}</button>
      <button id="zoom-reset" title="Fit to window">${ICONS.maximize}</button>
      <div class="sep"></div>
      <div id="draw-popup-wrap">
        <button id="draw-pen" title="Draw (right-click for options)" style="color:${penColor}">${ICONS.pencil}</button>
      </div>
      <button id="draw-eraser" title="Eraser">${ICONS.eraser}</button>
      <button id="draw-trash" title="Clear drawings" style="display:none">${ICONS.trash}</button>
      <div class="sep"></div>
      <button id="dark-toggle" title="Dark mode">${pdfDarkMode ? ICONS.sun : ICONS.moon}</button>
      <div class="sep"></div>
      <button id="close-btn" title="Close">${ICONS.close}</button>
    </div>
    <div id="scroller"><div id="page-wrap"${pdfDarkMode ? ' class="dark"' : ''}>
      <canvas id="page-canvas"></canvas>
      <canvas id="draw-canvas" style="pointer-events:none"></canvas>
      <div id="position-arrow-slot"></div>
      <div id="loading">Loading chart...</div>
    </div></div>`;
  updateTrashVisibility();

  // These two are independent -- rendering the PDF and fetching georef
  // status don't depend on each other -- so run them concurrently instead
  // of sequentially. Previously the geo-data fetch (and therefore the
  // position arrow) waited on the PDF render finishing first, meaning a
  // slow or stuck render silently blocked the arrow from ever appearing.
  const renderPromise = (async () => {
    try {
      const url = `/api/v1/charts/${icao}/export/${c.filename}`;
      pdfDoc = await pdfjsLib.getDocument({ url }).promise;
      pdfPage = await pdfDoc.getPage(1);
      updateZoomLabel();
      await recalcFitScale();
    } catch (e) {
      document.getElementById('scroller').innerHTML = `<div id="empty">Failed to load chart</div>`;
    }
  })();

  const geoPromise = (async () => {
    try {
      const res = await fetch(`/api/v1/charts/${icao}/data/${c.filename}`);
      if (res.ok) chartGeoStatus = await res.json();
    } catch (e) {
      chartGeoStatus = null;
    }
    scheduleCoord2Pixel();
  })();

  await Promise.all([renderPromise, geoPromise]);
}
</script>
</body></html>
"""

DEFAULT_CHARTS_DBF = r'C:\ProgramData\Jeppesen\Common\TerminalCharts\charts.dbf'
DEFAULT_VFR_DBF = r'C:\ProgramData\Jeppesen\Common\TerminalCharts\vfrchrts.dbf'
DEFAULT_TYPES_DBF = r'C:\ProgramData\Jeppesen\Common\TerminalCharts\ctypes.dbf'
DEFAULT_AIRPORTS_DBF = r'C:\ProgramData\Jeppesen\Common\TerminalCharts\Airports.dbf'
DEFAULT_TCL_DIR = os.path.join(REPO_ROOT, 'TCLs')


class ChartCatalog:
    def __init__(self, charts_dbf: str, vfr_dbf: str, types_dbf: str, tcl_dir: str) -> None:
        self.tcl_dir = os.path.abspath(tcl_dir)
        self.tcl2emf_exe = os.path.join(REPO_ROOT, 'bin', 'tcl2emf.exe')
        self.georef_tool_exe = os.path.join(REPO_ROOT, 'bin', 'georef_tool.exe')
        self.fixup_script = os.path.join(REPO_ROOT, 'tools', 'pdf_fixup_threshold.py')
        self.cache_dir = os.path.join(REPO_ROOT, 'cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        self.chart_dims: dict[str, tuple[int, int]] = {}
        self.georef_status_cache: dict[str, dict] = {}

        self._load_types(types_dbf)
        self._load_tcl_names(tcl_dir)
        self.charts_by_icao: dict[str, list[dict]] = {}
        self._load_charts(charts_dbf, is_vfr=False)
        self._load_charts(vfr_dbf, is_vfr=True)
        for records in self.charts_by_icao.values():
            records.sort(key=chart_sort_key)

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
                    'index_no': index_no,
                    'proc_id': proc_id,
                    'date_eff': date_eff.isoformat() if date_eff else '',
                    'sheet_id': sheet_id,
                    'has_tcl': filename.upper() in self.tcl_names,
                    'is_vfr': is_vfr,
                }
                self.charts_by_icao.setdefault(icao, []).append(record)

    def get_charts(self, icao: str, type_filter: str | None, search: str | None) -> list[dict]:
        # VFR charts come from VFRCharts.bin, which tcl_extract.py doesn't
        # pull TCLs from yet, so they always 502 on render. Hide them until
        # that's wired up rather than showing charts that can't open.
        records = [r for r in self.charts_by_icao.get(icao.upper(), []) if not r['is_vfr']]

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
        least once."""
        width, height = self.chart_dims.get(filename, (0, 0))
        return {
            'filename': filename,
            'icao': icao.upper(),
            'has_tcl': filename.upper() in self.tcl_names,
            'width': width,
            'height': height,
            'georef': self.georef_status(filename),
        }

    def _run_georef_tool(self, *args: str) -> dict:
        """georef_tool.exe wraps mrvtcl.dll (LoadLibraryA'd at runtime, not a
        static import) -- it needs mrvtcl.dll/mrvdrv.dll/zlib.dll sitting
        next to it in bin/. Returns {} on any failure (missing DLLs, bad TCL
        path, timeout) rather than raising, so callers can treat that the
        same as "not georeferenced"."""
        tcl_path = os.path.join(self.tcl_dir, f'{args[0]}.TCL')
        if not os.path.exists(tcl_path):
            return {}
        try:
            result = subprocess.run(
                [self.georef_tool_exe, args[1], tcl_path, *args[2:]],
                capture_output=True, text=True, timeout=10,
            )
            return json.loads(result.stdout)
        except Exception as exc:
            print(f"georef_tool failed for {args}: {exc!r}", file=sys.stderr)
            return {}

    def georef_status(self, filename: str) -> dict:
        if filename not in self.georef_status_cache:
            result = self._run_georef_tool(filename, 'status')
            self.georef_status_cache[filename] = result if result else {'georeferenced': False}
        return self.georef_status_cache[filename]

    def coord2pixel(self, filename: str, lat: float, lon: float) -> dict:
        result = self._run_georef_tool(filename, 'coord2pixel', f'{lat:.10f}', f'{lon:.10f}')
        return result if result else {'x': 0, 'y': 0, 'error': 'georef unavailable'}

    def pixel2coord(self, filename: str, x: int, y: int) -> dict:
        result = self._run_georef_tool(filename, 'pixel2coord', str(x), str(y))
        return result if result else {'latitude': 0, 'longitude': 0, 'error': 'georef unavailable'}

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


# The web viewer polls this every second for as long as any tab has it
# open, forever -- that's by design (keeps the aircraft arrow live), not a
# bug, but it drowns out every other log line if we print it like a normal
# request.
QUIET_PATHS = {'/api/v1/aircraft/position'}


def make_handler(catalog: ChartCatalog, position_store: PositionStore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if self.path in QUIET_PATHS:
                return
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.end_headers()

        def do_POST(self) -> None:
            start = time.monotonic()
            parsed = urllib.parse.urlsplit(self.path)
            parts = [p for p in parsed.path.split('/') if p]
            try:
                is_geo = (len(parts) == 7 and parts[:2] == ['api', 'v1'] and parts[2] == 'charts'
                          and parts[4] == 'geo')
                if is_geo and parts[5] == 'coord2pixel':
                    filename = parts[6]
                    length = int(self.headers.get('Content-Length', 0))
                    body = json.loads(self.rfile.read(length) or b'{}')
                    self._json(200, catalog.coord2pixel(filename, body.get('latitude', 0), body.get('longitude', 0)))
                elif is_geo and parts[5] == 'pixel2coord':
                    filename = parts[6]
                    length = int(self.headers.get('Content-Length', 0))
                    body = json.loads(self.rfile.read(length) or b'{}')
                    self._json(200, catalog.pixel2coord(filename, body.get('x', 0), body.get('y', 0)))
                else:
                    self._json(404, {'error': 'not found'})
            finally:
                if self.path not in QUIET_PATHS:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    print(f"{self.command} {self.path} -> {elapsed_ms:.1f}ms", file=sys.stderr)

        def do_GET(self) -> None:
            start = time.monotonic()
            parsed = urllib.parse.urlsplit(self.path)
            parts = [p for p in parsed.path.split('/') if p]
            qs = urllib.parse.parse_qs(parsed.query)

            try:
                if parsed.path == '/':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(INDEX_HTML)))
                    self.end_headers()
                    self.wfile.write(INDEX_HTML)
                elif parsed.path == '/health':
                    self._json(200, {'status': 'ok'})
                elif len(parts) == 2 and parts[0] == 'vendor' and FILENAME_RE.match(parts[1].replace('.', '_')):
                    vendor_path = os.path.join(REPO_ROOT, 'vendor', parts[1])
                    if not os.path.isfile(vendor_path):
                        self._json(404, {'error': 'not found'})
                    else:
                        with open(vendor_path, 'rb') as fd:
                            vendor_bytes = fd.read()
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/javascript; charset=utf-8')
                        self.send_header('Content-Length', str(len(vendor_bytes)))
                        self.end_headers()
                        self.wfile.write(vendor_bytes)
                elif parts == ['api', 'v1', 'aircraft', 'position']:
                    position = position_store.get()
                    self._json(200, position if position else {})
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
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.end_headers()
                            self.wfile.write(pdf_bytes)
                else:
                    self._json(404, {'error': 'not found'})
            finally:
                if self.path not in QUIET_PATHS:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    print(f"{self.command} {self.path} -> {elapsed_ms:.1f}ms", file=sys.stderr)

    return Handler


class QuietErrorMixin:
    """A client that navigates away / cancels a request mid-response (e.g.
    while a chart PDF is still streaming) makes the socket write raise
    ConnectionAbortedError/ConnectionResetError/BrokenPipeError. That's normal
    client behavior, not a server bug -- don't dump a traceback for it."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class DualStackServer(QuietErrorMixin, ThreadingHTTPServer):
    """Binds one IPv6 socket that also accepts IPv4 (like Go's net.Listen does
    for "0.0.0.0"). Without this, Python's HTTPServer is IPv4-only, so on
    Windows a client resolving "localhost" to ::1 first stalls for ~10s
    waiting on the IPv6 connection before falling back to IPv4."""
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        with contextlib.suppress(Exception):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class QuietThreadingHTTPServer(QuietErrorMixin, ThreadingHTTPServer):
    pass


def make_server(host: str, port: int, handler):
    if host in ('0.0.0.0', '', '::'):
        return DualStackServer(('::', port), handler)
    return QuietThreadingHTTPServer((host, port), handler)


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
    parser.add_argument('-no-msfs', '--no-msfs', action='store_true', help="Disable the MSFS SimConnect position feed")
    parser.add_argument('-no-xplane', '--no-xplane', action='store_true', help="Disable the X-Plane UDP position feed")
    parser.add_argument('-xplane-host', '--xplane-host', default=os.environ.get('XPLANE_HOST', '127.0.0.1'))
    parser.add_argument('-xplane-port', '--xplane-port', type=int, default=int(os.environ.get('XPLANE_PORT', 49000)))
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

    position_store = PositionStore()
    if not args.no_msfs:
        start_msfs_feed(position_store)
        print("MSFS SimConnect feed: enabled (retries until MSFS is running)")
    if not args.no_xplane:
        start_xplane_feed(position_store, args.xplane_host, args.xplane_port)
        print(f"X-Plane UDP feed: enabled ({args.xplane_host}:{args.xplane_port})")

    server = make_server(args.host, args.port, make_handler(catalog, position_store))
    print(f"Starting JeppQuery API server on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
