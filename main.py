#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPXEditor — single-file GPX track editor with:
- Rectangle and Lasso selection
- Start/End trimming with configurable X points (GUI + shortcuts)
- Copy selected coordinates to clipboard (lat, lon)
- Undo/Redo for all edits
- Basemap toggle, map preview in browser
- Right-button pan, scroll zoom, drag selected points
Tested on Python 3.10+ with QtAgg backend.
Dependencies: matplotlib (QtAgg), PySide6, gpxpy, folium, contextily, pyproj, numpy (optional: scipy for KDTree)
"""
import matplotlib
matplotlib.use("QtAgg")

import matplotlib.pyplot as plt

import sys
import os
import atexit
import builtins
import copy
import importlib.util
import inspect
import math
import webbrowser
import ctypes
import subprocess
import tempfile
import traceback
import time
from types import SimpleNamespace

import numpy as np
import gpxpy
import gpxpy.gpx
import folium
import contextily as ctx
from pyproj import Transformer

from PySide6 import QtCore, QtGui, QtWidgets

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.widgets import RectangleSelector, LassoSelector
from matplotlib.backend_bases import MouseButton
from matplotlib.path import Path

try:
    from scipy.spatial import cKDTree as KDTree
except Exception:
    KDTree = None


APP_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "GPXEditor_icon.ico")
WINDOWS_APP_ID = "surfplorer.GPXEditor"


ORIGINAL_PRINT = builtins.print
PRINT_LISTENERS = []


def add_print_listener(callback):
    if callback not in PRINT_LISTENERS:
        PRINT_LISTENERS.append(callback)


def remove_print_listener(callback):
    if callback in PRINT_LISTENERS:
        PRINT_LISTENERS.remove(callback)


def safe_print(*args, **kwargs):
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    file = kwargs.get("file", sys.stdout)
    message = sep.join(str(arg) for arg in args) + end
    try:
        ORIGINAL_PRINT(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(file, "encoding", None) or "utf-8"
        file.write(message.encode(encoding, errors="replace").decode(encoding, errors="replace"))
        if kwargs.get("flush"):
            file.flush()
    if file in (sys.stdout, sys.stderr):
        for callback in list(PRINT_LISTENERS):
            try:
                callback(message)
            except RuntimeError:
                remove_print_listener(callback)
            except Exception:
                pass


print = safe_print


class GPXEditor:
    def __init__(self, fig, ax, canvas):
        # --- dane GPX / stan ---
        self.x = np.array([], dtype=float)   # EPSG:3857
        self.y = np.array([], dtype=float)
        self.selected = set()
        self.gpx = None
        self.track = None
        self.segment = None
        self.point_metadata = []
        self.gpx_loaded = False
        self.kdtree = None
        self.current_path = None
        self.recent_files = []
        self._track_duration_cache = None

        # --- podkład mapowy ---
        self.basemap_enabled = True
        self.basemap_loaded = False
        self.basemap_img = None
        self.basemap_extent = None
        self.basemap_artist = None

        # --- widok / interakcja ---
        self.xlim_current = None
        self.ylim_current = None
        self.freeze_view = False
        self.dragged = False
        self.drag_origin = None
        self.last_canvas_xy = None
        self.pending_drag = False
        self.press_idx = None
        self.press_key = None
        self.press_canvas_xy = None
        self.press_on_selected = False
        self.hover_idx = None
        self.pick_tolerance_m = 2.0
        self.hover_annotation = None
        self.hover_marker = None
        self.track_line = None
        self.scatter = None
        self._last_pan_redraw = 0.0
        self._selector_disabled_for_drag = None

        # --- undo/redo ---
        self._undo = []
        self._redo = []
        self._undo_limit = 100

        # --- transformacje ---
        self.to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        self.to_wgs84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

        # --- Qt app (dialogs + clipboard) ---
        self.qt_app = QtWidgets.QApplication.instance()

        # --- GUI ---
        self.fig = fig
        self.canvas = canvas
        
        # Clear the figure and use the full canvas for the map view.
        self.fig.clear()
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(bottom=0.08, left=0.04, right=0.995, top=0.985)
        self.hover_annotation = self.ax.annotate(
            "",
            xy=(0, 0),
            xytext=(12, 12),
            textcoords="offset points",
            fontsize=9,
            color="black",
            bbox=dict(boxstyle="round", facecolor="white", alpha=0.9, edgecolor="gray"),
            arrowprops=dict(arrowstyle="->", color="gray", lw=0.8),
            zorder=6,
        )
        self.hover_annotation.set_visible(False)
        self._connect_events()
        self._update_plot(full=True)
        self._set_title()
        self.cut_input = None

    # -------------------------- GUI helpers --------------------------

    def _set_title(self):
        try:
            self.fig.canvas.manager.set_window_title("GPXEditor — by surfplorer")
        except Exception:
            pass
        self.ax.set_title("")

    def _connect_events(self):
        c = self.canvas.mpl_connect
        c('button_press_event', self._on_press)
        c('motion_notify_event', self._on_motion)
        c('button_release_event', self._on_release)
        c('scroll_event', self._on_scroll)
        c('key_press_event', self._on_key)
        self.rect_selector = None
        self.lasso_selector = None

    def _capture_current_view(self):
        if self.ax is None or not self.gpx_loaded or self.x.size == 0:
            return False
        try:
            self.xlim_current = tuple(float(v) for v in self.ax.get_xlim())
            self.ylim_current = tuple(float(v) for v in self.ax.get_ylim())
            return True
        except Exception:
            return False

    def set_freeze_view(self, enabled):
        self.freeze_view = bool(enabled)
        if self.freeze_view:
            self._capture_current_view()

    def set_pick_tolerance(self, meters):
        self.pick_tolerance_m = max(0.1, float(meters))

    # -------------------------- GPX I/O --------------------------

    def load_gpx(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            None,
            "Open GPX",
            "",
            "GPX files (*.gpx)",
        )
        if not path:
            return False

        return self.load_gpx_from_path(path)

    def reload_current_gpx(self):
        if not self.current_path:
            print("⚠️ No GPX file loaded to refresh.")
            return False
        return self.load_gpx_from_path(self.current_path)

    def load_gpx_from_path(self, path):
        path = os.path.abspath(path)
        if not path or not os.path.exists(path):
            print(f"⚠️ File not found: {path}")
            return False

        if self.freeze_view:
            self._capture_current_view()

        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.gpx = gpxpy.parse(f)
        except Exception as e:
            print(f"❌ Failed to open GPX: {e}")
            return False

        # Wybierz największy segment w całym pliku
        best_seg = None
        best_len = -1
        best_track = None
        for tr in self.gpx.tracks:
            for seg in tr.segments:
                if len(seg.points) > best_len:
                    best_len = len(seg.points)
                    best_seg = seg
                    best_track = tr

        if not best_seg or best_len < 2:
            print("❌ GPX does not contain a valid segment.")
            return False

        self.track = best_track
        self.segment = best_seg

        lons = [p.longitude for p in self.segment.points]
        lats = [p.latitude for p in self.segment.points]
        X, Y = self.to_merc.transform(lons, lats)
        self.x = np.asarray(X, dtype=float)
        self.y = np.asarray(Y, dtype=float)
        self.point_metadata = list(self.segment.points)
        self._refresh_track_duration_cache()

        self.selected.clear()
        self._redo.clear()
        self._undo.clear()
        self.gpx_loaded = True
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None

        # reset podkładu/widoku
        self.basemap_loaded = False
        self.basemap_img = None
        self.basemap_extent = None
        if not self.freeze_view or self.xlim_current is None or self.ylim_current is None:
            self.xlim_current = None
            self.ylim_current = None

        self._update_plot(full=True)
        self._update_info_text()
        self.current_path = path
        self._push_recent_file(path)
        print(f"✅ Loaded: {path} | points: {len(self.x)}")
        return True

    def sync_from_loaded_gpx(self, reset_history=False, reset_view=True):
        if self.gpx is None:
            print("⚠️ No GPX object loaded.")
            return False

        best_seg = None
        best_len = -1
        best_track = None
        for tr in self.gpx.tracks:
            for seg in tr.segments:
                if len(seg.points) > best_len:
                    best_len = len(seg.points)
                    best_seg = seg
                    best_track = tr

        if not best_seg or best_len < 2:
            print("❌ GPX does not contain a valid segment after script.")
            return False

        self.track = best_track
        self.segment = best_seg

        lons = [p.longitude for p in self.segment.points]
        lats = [p.latitude for p in self.segment.points]
        X, Y = self.to_merc.transform(lons, lats)
        self.x = np.asarray(X, dtype=float)
        self.y = np.asarray(Y, dtype=float)
        self.point_metadata = list(self.segment.points)
        self._refresh_track_duration_cache()

        self.selected.clear()
        if reset_history:
            self._redo.clear()
            self._undo.clear()
        self.gpx_loaded = True
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None

        self.basemap_loaded = False
        self.basemap_img = None
        self.basemap_extent = None
        if (reset_view and not self.freeze_view) or self.xlim_current is None or self.ylim_current is None:
            self.xlim_current = None
            self.ylim_current = None

        self._update_plot(full=True)
        self._update_info_text()
        return True

    def _push_recent_file(self, path):
        path = os.path.abspath(path)
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.insert(0, path)
        self.recent_files = self.recent_files[:10]

    def save_gpx(self):
        if not self.gpx_loaded:
            print("⚠️ Open a GPX file first")
            return

        lons, lats = self.to_wgs84.transform(self.x, self.y)

        new_points = []
        for i, (lon, lat) in enumerate(zip(lons, lats)):
            if i < len(self.point_metadata):
                p = self.point_metadata[i]
                new_points.append(gpxpy.gpx.GPXTrackPoint(
                    latitude=lat, longitude=lon,
                    elevation=p.elevation, time=p.time, symbol=p.symbol,
                    comment=p.comment, name=p.name
                ))
            else:
                new_points.append(gpxpy.gpx.GPXTrackPoint(latitude=lat, longitude=lon))

        self.segment.points = new_points
        self.point_metadata = list(new_points)
        self._refresh_track_duration_cache()

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            None,
            "Save as...",
            "edited.gpx",
            "GPX files (*.gpx)",
        )
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.gpx.to_xml())
            print("💾 Saved as:", path)
        else:
            print("❌ Save cancelled.")

    def save_simple_gpx_oneline(self):
        """Save GPX simplified: minimal format, 1 point = 1 line"""
        if not self.gpx_loaded:
            print("⚠️ Open a GPX file first")
            return

        lons, lats = self.to_wgs84.transform(self.x, self.y)

        lines = [
            '<gpx>',
            '<trk>',
            '<trkseg>',
            '',  # empty line
        ]
        
        for i, (lon, lat) in enumerate(zip(lons, lats)):
            time_str = ""
            if i < len(self.point_metadata):
                p = self.point_metadata[i]
                if p.time:
                    time_str = f"<time>{p.time.isoformat()}</time>"
            
            # Format: <trkpt lat="..." lon="..."><time>...</time></trkpt>
            if time_str:
                lines.append(f'<trkpt lat="{lat}" lon="{lon}">{time_str}</trkpt>')
            else:
                lines.append(f'<trkpt lat="{lat}" lon="{lon}"/>')

        lines.extend([
            '',  # empty line
            '</trkseg>',
            '</trk>',
            '</gpx>',
        ])

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            None,
            "Save simple GPX (one line per point) as...",
            "simple_oneline.gpx",
            "GPX files (*.gpx)",
        )
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            print("💾 Simple GPX (one line per point) saved as:", path)
        else:
            print("❌ Save cancelled.")

    def save_simple_gpx(self):
        """Save GPX with only basic data: lat, lon, time"""
        if not self.gpx_loaded:
            print("⚠️ Open a GPX file first")
            return

        lons, lats = self.to_wgs84.transform(self.x, self.y)

        # Build XML manually for simplified format
        xml_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="GPXEditor">',
            '  <trk>',
            '    <trkseg>',
        ]

        for i, (lon, lat) in enumerate(zip(lons, lats)):
            time_str = ""
            if i < len(self.point_metadata):
                p = self.point_metadata[i]
                if p.time:
                    # Format time in ISO 8601 format
                    time_str = f"<time>{p.time.isoformat()}</time>"

            xml_lines.append(f'      <trkpt lat="{lat}" lon="{lon}">')
            if time_str:
                xml_lines.append(f"        {time_str}")
            xml_lines.append('      </trkpt>')

        xml_lines.extend([
            '    </trkseg>',
            '  </trk>',
            '</gpx>',
        ])

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            None,
            "Save simple GPX as...",
            "simple.gpx",
            "GPX files (*.gpx)",
        )
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(xml_lines))
            print("💾 Simple GPX saved as:", path)
        else:
            print("❌ Save cancelled.")

    # -------------------------- Podkład / folium --------------------------

    def toggle_basemap(self):
        self.basemap_enabled = not self.basemap_enabled
        self._update_plot(full=True)
        print(f"🗺️ Basemap: {'ON' if self.basemap_enabled else 'OFF'}")

    def _ensure_basemap(self):
        if not self.basemap_enabled or self.basemap_loaded or self.x.size == 0:
            return
        try:
            pad = 50.0
            if self.xlim_current and self.ylim_current:
                x0, x1 = self.xlim_current
                y0, y1 = self.ylim_current
                x0, x1 = float(min(x0, x1) - pad), float(max(x0, x1) + pad)
                y0, y1 = float(min(y0, y1) - pad), float(max(y0, y1) + pad)
            else:
                x0, x1 = float(self.x.min() - pad), float(self.x.max() + pad)
                y0, y1 = float(self.y.min() - pad), float(self.y.max() + pad)
            zoom = self._choose_basemap_zoom(x0, y0, x1, y1)
            if zoom is None:
                print("⚠️ Basemap skipped: visible area is too large. Zoom in and enable map again.")
                self.basemap_enabled = False
                return
            self.basemap_img, self.basemap_extent = ctx.bounds2img(
                x0, y0, x1, y1, zoom=zoom, source=ctx.providers.OpenStreetMap.Mapnik
            )
            self.basemap_loaded = True
        except Exception as e:
            print("❌ Failed to load map:", e)
            self.basemap_enabled = False

    @staticmethod
    def _estimate_tile_count(x0, y0, x1, y1, zoom):
        world_width_m = 40075016.68557849
        tile_width_m = world_width_m / (2 ** zoom)
        cols = max(1, math.ceil(abs(x1 - x0) / tile_width_m))
        rows = max(1, math.ceil(abs(y1 - y0) / tile_width_m))
        return cols * rows

    def _choose_basemap_zoom(self, x0, y0, x1, y1):
        max_tiles = 64
        for zoom in range(17, 2, -1):
            if self._estimate_tile_count(x0, y0, x1, y1, zoom) <= max_tiles:
                return zoom
        return None

    def show_map(self, html_path="temp_map.html"):
        if self.x.size == 0:
            return
        lons, lats = self.to_wgs84.transform(self.x, self.y)
        points = list(zip(lats, lons))
        center = points[len(points) // 2]
        m = folium.Map(location=center, zoom_start=14)
        folium.PolyLine(points, weight=4).add_to(m)
        folium.Marker(points[0], tooltip="Start").add_to(m)
        folium.Marker(points[-1], tooltip="End").add_to(m)
        m.save(html_path)
        webbrowser.open("file://" + os.path.abspath(html_path), new=0)

    def open_in_notepad(self):
        """Open current GPX data in Notepad"""
        if not self.gpx_loaded:
            print("⚠️ Open a GPX file first")
            return

        lons, lats = self.to_wgs84.transform(self.x, self.y)

        # Build XML with current data
        xml_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="GPXEditor">',
            '  <trk>',
            '    <trkseg>',
        ]

        for i, (lon, lat) in enumerate(zip(lons, lats)):
            time_str = ""
            if i < len(self.point_metadata):
                p = self.point_metadata[i]
                if p.time:
                    time_str = f"<time>{p.time.isoformat()}</time>"

            xml_lines.append(f'      <trkpt lat="{lat}" lon="{lon}">')
            if time_str:
                xml_lines.append(f"        {time_str}")
            xml_lines.append('      </trkpt>')

        xml_lines.extend([
            '    </trkseg>',
            '  </trk>',
            '</gpx>',
        ])

        # Create temporary file
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.gpx', delete=False, encoding='utf-8') as f:
                temp_path = f.name
                f.write('\n'.join(xml_lines))
            
            # Open in Notepad
            subprocess.Popen(['notepad.exe', temp_path])
            print(f"📝 Opened in Notepad: {temp_path}")
        except Exception as e:
            print(f"❌ Error opening in Notepad: {e}")

    # -------------------------- Edycja / selekcja --------------------------

    def _push_undo(self):
        if self.x.size == 0:
            return
        self._undo.append((
            self.x.copy(),
            self.y.copy(),
            list(self.point_metadata),
            tuple(sorted(self.selected)),
        ))
        if len(self._undo) > self._undo_limit:
            self._undo.pop(0)
        self._redo.clear()

    def _undo_action(self):
        if not self._undo:
            return
        self._redo.append((
            self.x.copy(),
            self.y.copy(),
            list(self.point_metadata),
            tuple(sorted(self.selected)),
        ))
        x, y, point_metadata, sel = self._undo.pop()
        self.x, self.y = x, y
        self.point_metadata = list(point_metadata)
        self._refresh_track_duration_cache()
        self.selected = set(sel)
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()

    def _redo_action(self):
        if not self._redo:
            return
        self._undo.append((
            self.x.copy(),
            self.y.copy(),
            list(self.point_metadata),
            tuple(sorted(self.selected)),
        ))
        x, y, point_metadata, sel = self._redo.pop()
        self.x, self.y = x, y
        self.point_metadata = list(point_metadata)
        self._refresh_track_duration_cache()
        self.selected = set(sel)
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()

    def get_track_duration(self):
        return self._track_duration_cache

    def _refresh_track_duration_cache(self):
        self._track_duration_cache = self._calculate_track_duration()

    def _calculate_track_duration(self):
        times = [p.time for p in self.point_metadata if getattr(p, "time", None) is not None]
        if len(times) < 2:
            return None
        duration = max(times) - min(times)
        if duration.total_seconds() < 0:
            return None
        return duration

    @staticmethod
    def format_duration(duration):
        if duration is None:
            return "no data"
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours} h {minutes:02d} min {seconds:02d} s"
        if minutes:
            return f"{minutes} min {seconds:02d} s"
        return f"{seconds} s"

    def _format_point_hover_text(self, idx):
        if not (0 <= idx < len(self.point_metadata)):
            return ""
        point = self.point_metadata[idx]
        lon, lat = self.to_wgs84.transform(self.x[idx], self.y[idx])
        timestamp = point.time.isoformat(sep=" ", timespec="seconds") if point.time else "no timestamp"
        elevation = f"{point.elevation:.1f} m" if point.elevation is not None else "no elevation"
        return (
            f"#{idx}\n"
            f"time: {timestamp}\n"
            f"elev: {elevation}\n"
            f"lat: {lat:.6f}\n"
            f"lon: {lon:.6f}"
        )

    def _format_point_time_text(self, idx):
        if not (0 <= idx < len(self.point_metadata)):
            return ""
        point = self.point_metadata[idx]
        timestamp = point.time.isoformat(sep=" ", timespec="seconds") if point.time else "no timestamp"
        segment_elapsed = "no timestamp"
        start_time = None
        for candidate in self.point_metadata:
            if getattr(candidate, "time", None) is not None:
                start_time = candidate.time
                break
        if start_time is not None and point.time is not None:
            segment_elapsed = self.format_duration(point.time - start_time)
        return f"#{idx}\nglobal: {timestamp}\ntrack: {segment_elapsed}"

    def _update_hover_annotation(self):
        if self.hover_annotation is None:
            return
        if self.hover_idx is None or not (0 <= self.hover_idx < self.x.size):
            self.hover_annotation.set_visible(False)
            return
        self.hover_annotation.xy = (self.x[self.hover_idx], self.y[self.hover_idx])
        self.hover_annotation.set_text(self._format_point_time_text(self.hover_idx))
        self.hover_annotation.set_visible(True)

    def _update_hover_marker(self):
        if self.hover_marker is None:
            return
        if self.hover_idx is None or not (0 <= self.hover_idx < self.x.size):
            self.hover_marker.set_visible(False)
            return
        self.hover_marker.set_offsets([[self.x[self.hover_idx], self.y[self.hover_idx]]])
        self.hover_marker.set_visible(True)

    def _refresh_hover_display(self):
        self._update_hover_marker()
        self._update_hover_annotation()
        self.fig.canvas.draw_idle()

    def _nearest_index(self, x0, y0):
        if self.x.size == 0:
            return None, None
        if self.kdtree is not None:
            dist, idx = self.kdtree.query([x0, y0], k=1)
            return int(idx), float(dist)
        dx = self.x - x0
        dy = self.y - y0
        d2 = dx*dx + dy*dy
        idx = int(np.argmin(d2))
        return idx, float(np.sqrt(d2[idx]))

    # ---- prostokąt ----
    def activate_rectangle_selection(self):
        self._deactivate_selectors()
        self.rect_selector = RectangleSelector(
            self.ax, self._on_rect_select, useblit=True,
            button=[1], minspanx=5, minspany=5, spancoords='pixels', interactive=False
        )
        print("🔲 Mode: rectangle selection (click and drag)")

    def _on_rect_select(self, eclick, erelease):
        if eclick.xdata is None or eclick.ydata is None or erelease.xdata is None or erelease.ydata is None:
            return
        x0, x1 = sorted([eclick.xdata, erelease.xdata])
        y0, y1 = sorted([eclick.ydata, erelease.ydata])
        mask = (self.x >= x0) & (self.x <= x1) & (self.y >= y0) & (self.y <= y1)
        idxs = np.where(mask)[0].tolist()
        if idxs:
            self._push_undo()
            self.selected.update(idxs)
            print(f"🔲 Selected {len(idxs)} points (rectangle)")
            self._update_plot()
            self._update_info_text()

        elif self.selected and not (getattr(eclick, "key", None) == 'shift' or getattr(eclick, "key", None) == 'control'):
            self.clear_selection()

    # ---- lasso ----
    def activate_lasso_selection(self):
        self._deactivate_selectors()
        self.lasso_selector = LassoSelector(self.ax, onselect=self._on_lasso_select)
        print("✏️ Mode: lasso selection (draw around points)")

    def _on_lasso_select(self, verts):
        if not verts:
            return
        # LassoSelector passes vertices in data coordinates.
        poly_data = np.asarray(
            [(x, y) for x, y in verts if x is not None and y is not None],
            dtype=float,
        )
        if len(poly_data) < 3:
            return
        path = Path(poly_data, closed=True)
        pts = np.column_stack((self.x, self.y))
        inside = path.contains_points(pts)
        idxs = np.where(inside)[0].tolist()
        if idxs:
            self._push_undo()
            self.selected.update(idxs)
            print(f"✏️ Selected {len(idxs)} points (lasso)")
            self._update_plot()
            self._update_info_text()

        elif self.selected:
            self.clear_selection()

    def _deactivate_selectors(self):
        if self.rect_selector is not None:
            try:
                self.rect_selector.set_active(False)
            except Exception:
                pass
            self.rect_selector = None
        if self.lasso_selector is not None:
            try:
                self.lasso_selector.disconnect_events()
                self.lasso_selector = None
            except Exception:
                self.lasso_selector = None

    def _selection_tool_active(self):
        return self.rect_selector is not None or self.lasso_selector is not None

    # ---- kopiowanie współrzędnych ----
    def copy_selected_coords(self):
        if not self.selected:
            print("ℹ️ No selection.")
            return
        duration_text = self.format_duration(self.get_track_duration())
        lines = [
            f"Points: {int(self.x.size)}",
            f"Duration: {duration_text}",
        ]
        if self.selected:
            lines.append("")
            lines.append(f"Selection: {len(self.selected)} pts")
        for idx in sorted(self.selected):
            if 0 <= idx < self.x.size:
                lon, lat = self.to_wgs84.transform(self.x[idx], self.y[idx])
                lines.append(f"{lat:.7f}, {lon:.7f}")
        text = "\n".join(lines)
        try:
            clipboard = self.qt_app.clipboard()
            clipboard.setText(text)
            print("📋 Copied to clipboard:")
            print(text)
        except Exception as e:
            print("❌ Failed to copy to clipboard:", e)

    # ---- przycinanie ----
    def _remove_x_from(self, where):
        if self.cut_input is None:
            print("⚠️ Missing X input field.")
            return
        value = self.cut_input.text().strip()
        try:
            n = int(value)
        except Exception:
            print("⚠️ Enter an integer in the X field.")
            return
        if n <= 0:
            print("⚠️ X must be > 0.")
            return
        if where == 'start':
            self.remove_first_n(n)
        else:
            self.remove_last_n(n)

    def remove_first_n(self, n):
        if self.x.size <= 1:
            return
        n = max(0, min(n, int(self.x.size)-1))
        if n == 0:
            return
        self._push_undo()
        self.x = self.x[n:]
        self.y = self.y[n:]
        self.point_metadata = self.point_metadata[n:]
        self._refresh_track_duration_cache()
        # przesuń selekcję
        self.selected = {i - n for i in self.selected if i - n >= 0}
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()
        print(f"🔻 Removed the first {n} points")

    def remove_last_n(self, n):
        if self.x.size <= 1:
            return
        n = max(0, min(n, int(self.x.size)-1))
        if n == 0:
            return
        self._push_undo()
        self.x = self.x[:-n]
        self.y = self.y[:-n]
        self.point_metadata = self.point_metadata[:-n]
        self._refresh_track_duration_cache()
        self.selected = {i for i in self.selected if i < self.x.size}
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()
        print(f"🔺 Removed the last {n} points")

    def delete_selected(self):
        if not self.selected:
            return
        self._push_undo()
        mask = np.ones(self.x.size, dtype=bool)
        idxs = np.array(sorted(self.selected), dtype=int)
        mask[idxs] = False
        removed = len(idxs)
        self.x = self.x[mask]
        self.y = self.y[mask]
        self.point_metadata = [p for i, p in enumerate(self.point_metadata) if mask[i]]
        self._refresh_track_duration_cache()
        self.selected.clear()
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()
        print(f"🗑️ Removed {removed} points")

    def clear_selection(self):
        if not self.selected:
            return
        self.selected.clear()
        self._update_plot()
        self._update_info_text()
        print("Selection cleared")

    # -------------------------- Zdarzenia --------------------------

    def _on_press(self, event):
        if not self.gpx_loaded or event.xdata is None or event.ydata is None:
            return

        # PPM – pan
        if event.button == MouseButton.RIGHT:
            self.last_canvas_xy = (event.x, event.y)
            return

        if self._selection_tool_active() and event.button == MouseButton.LEFT:
            idx, dist = self._nearest_index(event.xdata, event.ydata)
            if self.rect_selector is not None and idx is not None and dist is not None and dist <= self.pick_tolerance_m:
                self.pending_drag = True
                self.press_idx = idx
                self.press_key = getattr(event, "key", None)
                self.press_canvas_xy = (event.x, event.y)
                self.press_on_selected = idx in self.selected
                self._selector_disabled_for_drag = self.rect_selector
                try:
                    self.rect_selector.set_active(False)
                except Exception:
                    pass
                return
            self.pending_drag = False
            self.press_idx = None
            self.press_key = None
            self.press_canvas_xy = None
            self.press_on_selected = False
            return

        # LPM – wybór/przeciąganie
        if event.button == MouseButton.LEFT:
            idx, dist = self._nearest_index(event.xdata, event.ydata)
            if idx is None:
                return
            if dist is None or dist > self.pick_tolerance_m:
                # klik w puste: wyczyść selekcję (chyba że trzymasz Shift/Ctrl)
                if not (getattr(event, "key", None) == 'shift' or getattr(event, "key", None) == 'control'):
                    self.clear_selection()
                self.pending_drag = False
                self.press_idx = None
                self.press_key = None
                self.press_canvas_xy = None
                self.press_on_selected = False
                return

            # Zapisz kliknięcie i wystartuj przeciąganie dopiero po progu
            self.pending_drag = True
            self.press_idx = idx
            self.press_key = event.key
            self.press_canvas_xy = (event.x, event.y)
            self.press_on_selected = idx in self.selected

    def _on_motion(self, event):
        if event.inaxes != self.ax and self.hover_idx is not None:
            self.hover_idx = None
            self._refresh_hover_display()
            return

        # pan PPM
        if event.button == MouseButton.RIGHT and self.last_canvas_xy and event.x is not None and event.y is not None:
            last_x, last_y = self.last_canvas_xy
            if event.x == last_x and event.y == last_y:
                return
            inv = self.ax.transData.inverted()
            x_prev, y_prev = inv.transform((last_x, last_y))
            x_curr, y_curr = inv.transform((event.x, event.y))
            dx = x_curr - x_prev
            dy = y_curr - y_prev
            if dx or dy:
                x0, x1 = self.ax.get_xlim()
                y0, y1 = self.ax.get_ylim()
                new_xlim = (x0 - dx, x1 - dx)
                new_ylim = (y0 - dy, y1 - dy)
                self.xlim_current = new_xlim
                self.ylim_current = new_ylim
                self.ax.set_xlim(*new_xlim)
                self.ax.set_ylim(*new_ylim)
            self.last_canvas_xy = (event.x, event.y)
            now = time.monotonic()
            if now - self._last_pan_redraw >= 0.05:
                self._last_pan_redraw = now
                self._update_plot()
            else:
                self.fig.canvas.draw_idle()
            return

        if not self.dragged and event.xdata is not None and event.ydata is not None:
            idx, dist = self._nearest_index(event.xdata, event.ydata)
            new_hover = idx if (dist is not None and dist <= self.pick_tolerance_m) else None
            if new_hover != self.hover_idx:
                self.hover_idx = new_hover
                self._refresh_hover_display()

        if self._selection_tool_active() and not (self.pending_drag or self.dragged):
            return

        if (
            self.pending_drag and not self.dragged and
            event.x is not None and event.y is not None and
            event.xdata is not None and event.ydata is not None
        ):
            dx_px = event.x - self.press_canvas_xy[0]
            dy_px = event.y - self.press_canvas_xy[1]
            if (dx_px * dx_px + dy_px * dy_px) >= 16:  # 4px próg
                if not self.press_on_selected or self.press_key in ('shift', 'control'):
                    self._apply_selection_click(self.press_idx, self.press_key)
                if self.selected:
                    self.dragged = True
                    self.drag_origin = (event.xdata, event.ydata)
                    self._push_undo()

        # przeciąganie zaznaczonych LPM
        if self.dragged and event.xdata is not None and event.ydata is not None:
            dx = event.xdata - self.drag_origin[0]
            dy = event.ydata - self.drag_origin[1]
            if self.selected:
                idxs = np.fromiter(self.selected, dtype=int)
                self.x[idxs] += dx
                self.y[idxs] += dy
            self.drag_origin = (event.xdata, event.ydata)
            self._update_plot()

    def _on_release(self, event):
        if self.pending_drag and not self.dragged and self.press_idx is not None:
            self._apply_selection_click(self.press_idx, self.press_key)
        if self._selector_disabled_for_drag is not None:
            try:
                self._selector_disabled_for_drag.set_active(True)
            except Exception:
                pass
            self._selector_disabled_for_drag = None
        self.dragged = False
        self.drag_origin = None
        self.last_canvas_xy = None
        self.pending_drag = False
        self.press_idx = None
        self.press_key = None
        self.press_canvas_xy = None
        self.press_on_selected = False
        if self.gpx_loaded and self.x.size:
            self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        if self._selection_tool_active():
            return

    def _on_scroll(self, event):
        if event.xdata is None or event.ydata is None:
            return
        base_scale = 1.1
        scale = base_scale if event.step < 0 else 1 / base_scale
        xdata, ydata = event.xdata, event.ydata
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        new_xlim = [xdata + (x - xdata) * scale for x in (x0, x1)]
        new_ylim = [ydata + (y - ydata) * scale for y in (y0, y1)]
        self.xlim_current = tuple(new_xlim)
        self.ylim_current = tuple(new_ylim)
        self.ax.set_xlim(new_xlim)
        self.ax.set_ylim(new_ylim)
        self._update_plot()

    def _apply_selection_click(self, idx, key):
        if idx is None:
            return
        if key == 'shift':
            if idx in self.selected:
                self.selected.remove(idx)
            else:
                self.selected.add(idx)
        elif key == 'control':
            if self.selected:
                a = min(self.selected)
                b = idx
                lo, hi = (a, b) if a <= b else (b, a)
                self.selected = set(range(lo, hi + 1))
            else:
                self.selected = {idx}
        else:
            self.selected = {idx}
        self._update_plot()
        self._update_info_text()

    def _on_key(self, event):
        if not event.key:
            return
        k = event.key.lower()

        if k == 'delete':
            self.delete_selected()
        elif k == 'ctrl+z':
            self._undo_action()
        elif k == 'ctrl+y':
            self._redo_action()
        elif k == 'r':
            self._reset_view()
        elif k == 'm':
            self.toggle_basemap()
        elif k == 'ctrl+s':
            self.save_gpx()
        elif k == 'ctrl+o':
            self.load_gpx()
        elif k == '[':
            self.remove_first_n(10)
        elif k == ']':
            self.remove_last_n(10)
        elif k == 'shift+[':
            self.remove_first_n(50)
        elif k == 'shift+]':
            self.remove_last_n(50)
        elif k == 'ctrl+[':
            self.remove_first_n(100)
        elif k == 'ctrl+]':
            self.remove_last_n(100)
        elif k == 'c':
            # skrót do kopiowania
            self.copy_selected_coords()

    # -------------------------- Rysowanie --------------------------

    def _set_default_view_limits(self):
        if not self.x.size:
            return False
        self.xlim_current = (float(self.x.min()) - 500, float(self.x.max()) + 500)
        self.ylim_current = (float(self.y.min()) - 500, float(self.y.max()) + 500)
        return True

    def _get_view_bounds(self, padding_ratio=0.08):
        if not (self.xlim_current and self.ylim_current):
            return None
        x0, x1 = self.xlim_current
        y0, y1 = self.ylim_current
        xmin, xmax = sorted((float(x0), float(x1)))
        ymin, ymax = sorted((float(y0), float(y1)))
        dx = xmax - xmin
        dy = ymax - ymin
        if dx > 0:
            xmin -= dx * padding_ratio
            xmax += dx * padding_ratio
        if dy > 0:
            ymin -= dy * padding_ratio
            ymax += dy * padding_ratio
        return xmin, xmax, ymin, ymax

    def _display_indices(self, max_points=8000, include_selected=False):
        if self.x.size == 0:
            return np.array([], dtype=int)

        bounds = self._get_view_bounds()
        if bounds is None:
            idxs = np.arange(self.x.size, dtype=int)
        else:
            xmin, xmax, ymin, ymax = bounds
            idxs = np.flatnonzero(
                (self.x >= xmin) & (self.x <= xmax) &
                (self.y >= ymin) & (self.y <= ymax)
            )
            if idxs.size == 0:
                idxs = np.arange(self.x.size, dtype=int)

        if idxs.size > max_points:
            step = int(math.ceil(idxs.size / max_points))
            idxs = idxs[::step]

        extras = []
        if include_selected and self.selected:
            extras.extend(i for i in self.selected if 0 <= i < self.x.size)
        if include_selected and self.hover_idx is not None and 0 <= self.hover_idx < self.x.size:
            extras.append(self.hover_idx)
        if extras:
            idxs = np.union1d(idxs, np.asarray(extras, dtype=int))

        return idxs

    def _reset_view(self):
        if self.x.size:
            self._set_default_view_limits()
            self.ax.set_xlim(*self.xlim_current)
            self.ax.set_ylim(*self.ylim_current)
            self.fig.canvas.draw_idle()

    def _update_plot(self, full=False):
        # Pełny redraw?
        if full:
            self.ax.clear()
            self.track_line = None
            self.scatter = None
            self.basemap_artist = None
            self.hover_annotation = None
            self.hover_marker = None

        if self.x.size and (self.xlim_current is None or self.ylim_current is None):
            self._set_default_view_limits()

        # Podkład
        if self.basemap_enabled:
            self._ensure_basemap()
            if self.basemap_img is not None and self.basemap_extent is not None:
                if self.basemap_artist is None:
                    self.basemap_artist = self.ax.imshow(
                        self.basemap_img,
                        extent=self.basemap_extent,
                        interpolation='nearest',
                        resample=False,
                        zorder=0
                    )
                else:
                    self.basemap_artist.set_data(self.basemap_img)
                    self.basemap_artist.set_extent(self.basemap_extent)
        elif self.basemap_artist is not None:
            self.basemap_artist.remove()
            self.basemap_artist = None

        # Ślad
        if self.x.size:
            line_idxs = self._display_indices(max_points=10000, include_selected=False)
            point_idxs = self._display_indices(max_points=8000, include_selected=True)
            line_x = self.x[line_idxs] if line_idxs.size else np.array([], dtype=float)
            line_y = self.y[line_idxs] if line_idxs.size else np.array([], dtype=float)
            point_offsets = (
                np.column_stack((self.x[point_idxs], self.y[point_idxs]))
                if point_idxs.size else np.empty((0, 2), dtype=float)
            )

            colors = np.full(point_idxs.size, 'red', dtype=object)
            if self.selected and point_idxs.size:
                selected_idxs = np.fromiter(self.selected, dtype=int)
                colors[np.isin(point_idxs, selected_idxs)] = 'green'
            if self.track_line is None:
                (self.track_line,) = self.ax.plot(line_x, line_y, '-', zorder=4)
            else:
                self.track_line.set_data(line_x, line_y)

            if self.scatter is None:
                point_size = 6 if self.x.size > 20000 else 14
                self.scatter = self.ax.scatter(
                    point_offsets[:, 0], point_offsets[:, 1],
                    c=colors,
                    s=point_size,
                    zorder=5,
                    rasterized=True
                )
            else:
                self.scatter.set_offsets(point_offsets)
                self.scatter.set_facecolor(colors)

            if self.hover_marker is None:
                self.hover_marker = self.ax.scatter(
                    [], [],
                    c='orange',
                    edgecolors='black',
                    linewidths=0.7,
                    s=48,
                    zorder=7,
                )
            self._update_hover_marker()

            if self.xlim_current and self.ylim_current:
                self.ax.set_xlim(*self.xlim_current)
                self.ax.set_ylim(*self.ylim_current)
            else:
                self._reset_view()


        if self.hover_annotation is None:
            self.hover_annotation = self.ax.annotate(
                "",
                xy=(0, 0),
                xytext=(12, 12),
                textcoords="offset points",
                fontsize=9,
                color="black",
                bbox=dict(boxstyle="round", facecolor="white", alpha=0.9, edgecolor="gray"),
                arrowprops=dict(arrowstyle="->", color="gray", lw=0.8),
                zorder=6,
            )
        self._update_hover_annotation()

        # info box – odtwórz box po clear()
        self._set_title()
        self.fig.canvas.draw_idle()

    def _update_info_text(self):
        return
        if not self.gpx_loaded:
            self.info_text.set_text("")
            self.fig.canvas.draw_idle()
            return

        duration_text = self.format_duration(self.get_track_duration())
        lines = [
            f"Punkty: {int(self.x.size)}",
            f"Czas trwania: {duration_text}",
        ]
        if self.selected:
            lines.append("")
            lines.append(f"Zaznaczenie: {len(self.selected)} pkt")
        for idx in sorted(self.selected):
            if 0 <= idx < len(self.point_metadata):
                p = self.point_metadata[idx]
                t = p.time.isoformat() if p.time else "—"
                ele = f"{p.elevation:.1f} m" if p.elevation is not None else "—"
                # lat/lon po aktualnej edycji
                lon, lat = self.to_wgs84.transform(self.x[idx], self.y[idx])
                lines.append(f"#{idx} — lat={lat:.7f}, lon={lon:.7f} | t={t} | ele={ele}")
        self.info_text.set_text("\n".join(lines))
        self.fig.canvas.draw_idle()


class MainWindow(QtWidgets.QMainWindow):
    terminal_log_signal = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GPXEditor — by surfplorer")
        if os.path.exists(APP_ICON_PATH):
            self.setWindowIcon(QtGui.QIcon(APP_ICON_PATH))
        self.resize(1560, 940)
        self.setAcceptDrops(True)
        self._selection_mode = "Single point"
        self.scripts_dir = None
        self.project_dir = os.path.dirname(os.path.abspath(__file__))
        self.terminal_cwd = self.project_dir
        self.terminal_process = None
        self._print_listener = None
        self.terminal_log_signal.connect(self._append_terminal_log)
        self._apply_theme()
        self._build_ui()
        self._print_listener = lambda message: self.terminal_log_signal.emit(message)
        add_print_listener(self._print_listener)
        self._append_terminal_log(f"Terminal ready: {self.terminal_cwd}")

    def _apply_theme(self):
        self.setStyleSheet(
            """
            QWidget {
                background: #f5f7fb;
                color: #1f2933;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #d6dbe1;
                border-radius: 4px;
                padding: 4px 7px;
            }
            QPushButton:hover {
                background: #eef2f6;
            }
            QPushButton:pressed {
                background: #e1e6eb;
            }
            QPushButton#freezeViewButton {
                background: #ffffff;
                border: 2px solid #8fa1b3;
                border-radius: 6px;
                color: #1f2933;
                font-weight: 700;
                padding: 5px 8px;
                text-align: left;
            }
            QPushButton#freezeViewButton:hover {
                background: #eef2f6;
                border-color: #2c6fb7;
            }
            QPushButton#freezeViewButton:checked {
                background: #1f7a4d;
                border-color: #145c39;
                color: #ffffff;
            }
            QPushButton#freezeViewButton:checked:hover {
                background: #238a58;
                border-color: #145c39;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background: #ffffff;
                border: 1px solid #d6dbe1;
                border-radius: 4px;
                padding: 3px 6px;
            }
            QPlainTextEdit#terminalOutput {
                background: #ffffff;
                color: #1f2933;
                border: 1px solid #d6dbe1;
                border-radius: 6px;
                padding: 6px;
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 10pt;
            }
            QLineEdit#terminalInput {
                background: #ffffff;
                border: 1px solid #94a3b8;
                border-radius: 4px;
                padding: 3px 6px;
                font-family: Consolas, "Cascadia Mono", monospace;
            }
            QRadioButton {
                spacing: 7px;
                padding: 4px 7px;
                border: 1px solid transparent;
                border-radius: 4px;
            }
            QRadioButton:hover {
                background: #eef2f6;
                border-color: #d6dbe1;
            }
            QRadioButton:checked {
                background: #dcecff;
                border-color: #7aa7d9;
                font-weight: 600;
            }
            QRadioButton::indicator {
                width: 13px;
                height: 13px;
            }
            QRadioButton::indicator:unchecked {
                border: 2px solid #8fa1b3;
                border-radius: 8px;
                background: #ffffff;
            }
            QRadioButton::indicator:checked {
                border: 2px solid #2c6fb7;
                border-radius: 8px;
                background: #2c6fb7;
            }
            QGroupBox {
                border: 1px solid #d6dbe1;
                border-radius: 6px;
                margin-top: 8px;
                padding: 4px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
            }
            """
        )

    def _build_ui(self):
        central = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        figure = Figure(figsize=(12, 8), facecolor="#ffffff")
        canvas = FigureCanvas(figure)

        self.editor = GPXEditor(figure, None, canvas)

        self._build_toolbar()

        # ============================================================
        # LEFT PANEL
        # ============================================================

        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_panel.setFixedWidth(230)

        # ---------- File ----------
        group_file = QtWidgets.QGroupBox("File")
        group_file_layout = QtWidgets.QVBoxLayout(group_file)
        group_file_layout.setSpacing(4)

        btn_open = QtWidgets.QPushButton("Open GPX")
        btn_open.clicked.connect(self._open_gpx_dialog)

        btn_refresh_current = QtWidgets.QPushButton("Refresh current GPX")
        btn_refresh_current.clicked.connect(self._refresh_current_gpx)

        self.recent_files_combo = QtWidgets.QComboBox()
        self.recent_files_combo.setMinimumWidth(180)
        self.recent_files_combo.setEnabled(False)
        self.recent_files_combo.activated.connect(self._open_recent_file)

        btn_save = QtWidgets.QPushButton("Save…")
        btn_save.clicked.connect(self.editor.save_gpx)

        btn_save_simple_oneline = QtWidgets.QPushButton("Save simple (1 line/point)")
        btn_save_simple_oneline.clicked.connect(self.editor.save_simple_gpx_oneline)

        group_file_layout.addWidget(btn_open)
        group_file_layout.addWidget(btn_refresh_current)
        group_file_layout.addWidget(QtWidgets.QLabel("Recent:"))
        group_file_layout.addWidget(self.recent_files_combo)
        group_file_layout.addWidget(btn_save)
        group_file_layout.addWidget(btn_save_simple_oneline)

        # ---------- Delete points ----------
        group_delete = QtWidgets.QGroupBox("Delete points")
        group_delete_layout = QtWidgets.QVBoxLayout(group_delete)
        group_delete_layout.setSpacing(4)

        btn_cut_start = QtWidgets.QPushButton("Remove 1 from start")
        btn_cut_start.clicked.connect(lambda: self.editor.remove_first_n(1))

        btn_cut_end = QtWidgets.QPushButton("Remove 1 from end")
        btn_cut_end.clicked.connect(lambda: self.editor.remove_last_n(1))

        cut_input = QtWidgets.QSpinBox()
        cut_input.setRange(1, 999999)
        cut_input.setValue(10)

        btn_cut_x_start = QtWidgets.QPushButton("Remove X from start")
        btn_cut_x_start.clicked.connect(lambda: self.editor._remove_x_from("start"))

        btn_cut_x_end = QtWidgets.QPushButton("Remove X from end")
        btn_cut_x_end.clicked.connect(lambda: self.editor._remove_x_from("end"))

        group_delete_layout.addWidget(btn_cut_start)
        group_delete_layout.addWidget(btn_cut_end)
        group_delete_layout.addWidget(QtWidgets.QLabel("X:"))
        group_delete_layout.addWidget(cut_input)
        group_delete_layout.addWidget(btn_cut_x_start)
        group_delete_layout.addWidget(btn_cut_x_end)

        # ---------- Selection mode ----------
        group_selection = QtWidgets.QGroupBox("Selection mode")
        group_selection_layout = QtWidgets.QVBoxLayout(group_selection)
        group_selection_layout.setSpacing(4)

        radio_single = QtWidgets.QRadioButton("Single point")
        radio_rect = QtWidgets.QRadioButton("Rectangle")
        radio_lasso = QtWidgets.QRadioButton("Lasso")

        radio_single.setChecked(True)

        radio_single.toggled.connect(
            lambda checked: checked and self._set_selection_mode("Single point")
        )
        radio_rect.toggled.connect(
            lambda checked: checked and self._set_selection_mode("Rectangle")
        )
        radio_lasso.toggled.connect(
            lambda checked: checked and self._set_selection_mode("Lasso")
        )
        group_selection_layout.addWidget(radio_single)
        group_selection_layout.addWidget(radio_rect)
        group_selection_layout.addWidget(radio_lasso)

        self.pick_tolerance_input = QtWidgets.QDoubleSpinBox()
        self.pick_tolerance_input.setRange(0.1, 1000.0)
        self.pick_tolerance_input.setDecimals(1)
        self.pick_tolerance_input.setSingleStep(1.0)
        self.pick_tolerance_input.setValue(self.editor.pick_tolerance_m)
        self.pick_tolerance_input.setSuffix(" m")
        self.pick_tolerance_input.setToolTip("Distance from cursor to point required for single-point selection and hover.")
        self.pick_tolerance_input.valueChanged.connect(self._set_pick_tolerance)

        group_selection_layout.addWidget(QtWidgets.QLabel("Point pick radius:"))
        group_selection_layout.addWidget(self.pick_tolerance_input)

        # ---------- Selection edit ----------
        group_selection_edit = QtWidgets.QGroupBox("Selection edit")
        group_selection_edit_layout = QtWidgets.QVBoxLayout(group_selection_edit)
        group_selection_edit_layout.setSpacing(4)

        btn_delete_selected = QtWidgets.QPushButton("Delete selected")
        btn_delete_selected.clicked.connect(self.editor.delete_selected)

        btn_clear_selection = QtWidgets.QPushButton("Clear selection")
        btn_clear_selection.clicked.connect(self._clear_selection)

        group_selection_edit_layout.addWidget(btn_delete_selected)
        group_selection_edit_layout.addWidget(btn_clear_selection)

        # ---------- View / tools ----------
        group_view = QtWidgets.QGroupBox("View / tools")
        group_view_layout = QtWidgets.QVBoxLayout(group_view)
        group_view_layout.setSpacing(4)

        btn_reset_view = QtWidgets.QPushButton("Reset view")
        btn_reset_view.clicked.connect(self.editor._reset_view)

        self.freeze_view_button = QtWidgets.QPushButton("Freeze view: OFF")
        self.freeze_view_button.setObjectName("freezeViewButton")
        self.freeze_view_button.setCheckable(True)
        self.freeze_view_button.setMinimumHeight(32)
        self.freeze_view_button.setToolTip("Keep current zoom and pan when opening or refreshing GPX files.")
        self.freeze_view_button.toggled.connect(self._set_freeze_view)

        btn_copy = QtWidgets.QPushButton("Copy coordinates")
        btn_copy.clicked.connect(self.editor.copy_selected_coords)

        btn_preview = QtWidgets.QPushButton("Open in browser")
        btn_preview.clicked.connect(lambda: self.editor.show_map())

        btn_notepad = QtWidgets.QPushButton("Open in Notepad")
        btn_notepad.clicked.connect(self.editor.open_in_notepad)

        group_view_layout.addWidget(btn_reset_view)
        group_view_layout.addWidget(self.freeze_view_button)
        group_view_layout.addWidget(btn_copy)
        group_view_layout.addWidget(btn_preview)
        group_view_layout.addWidget(btn_notepad)

        # Add groups to left panel
        left_layout.addWidget(group_file)
        left_layout.addWidget(group_delete)
        left_layout.addWidget(group_selection)
        left_layout.addWidget(group_selection_edit)
        left_layout.addWidget(group_view)
        left_layout.addStretch()

        self.editor.cut_input = cut_input

        # ============================================================
        # CENTER CANVAS
        # ============================================================

        canvas_container = QtWidgets.QWidget()
        canvas_layout = QtWidgets.QVBoxLayout(canvas_container)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.addWidget(canvas, stretch=1)

        # ============================================================
        # RIGHT PANEL
        # ============================================================

        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_panel.setFixedWidth(360)

        group_scripts = QtWidgets.QGroupBox("Custom Scripts")
        group_scripts_layout = QtWidgets.QVBoxLayout(group_scripts)
        group_scripts_layout.setSpacing(4)

        btn_select_dir = QtWidgets.QPushButton("Select Scripts Directory")
        btn_select_dir.clicked.connect(self._select_scripts_directory)

        btn_scripts_help = QtWidgets.QPushButton("Script API help")
        btn_scripts_help.clicked.connect(self._show_custom_scripts_help)

        self.scripts_dir_label = QtWidgets.QLabel("No scripts folder selected")
        self.scripts_dir_label.setWordWrap(True)

        self.scripts_container = QtWidgets.QVBoxLayout()

        terminal_header = QtWidgets.QHBoxLayout()
        terminal_label = QtWidgets.QLabel("Logs / PowerShell")
        btn_terminal_clear = QtWidgets.QPushButton("Clear")
        btn_terminal_clear.clicked.connect(self._clear_terminal_log)
        terminal_header.addWidget(terminal_label)
        terminal_header.addStretch()
        terminal_header.addWidget(btn_terminal_clear)

        self.terminal_cwd_label = QtWidgets.QLabel(self.terminal_cwd)
        self.terminal_cwd_label.setWordWrap(True)
        self.terminal_cwd_label.setToolTip(self.terminal_cwd)

        self.terminal_output = QtWidgets.QPlainTextEdit()
        self.terminal_output.setObjectName("terminalOutput")
        self.terminal_output.setReadOnly(True)
        self.terminal_output.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.terminal_output.setMaximumBlockCount(3000)
        self.terminal_output.setMinimumHeight(260)

        self.terminal_input = QtWidgets.QLineEdit()
        self.terminal_input.setObjectName("terminalInput")
        self.terminal_input.setPlaceholderText("PowerShell command...")
        self.terminal_input.returnPressed.connect(self._run_terminal_command)

        self.terminal_run_btn = QtWidgets.QPushButton("Run")
        self.terminal_run_btn.clicked.connect(self._run_terminal_command)

        self.terminal_stop_btn = QtWidgets.QPushButton("Stop")
        self.terminal_stop_btn.setEnabled(False)
        self.terminal_stop_btn.clicked.connect(self._stop_terminal_command)

        terminal_command_layout = QtWidgets.QHBoxLayout()
        terminal_command_layout.addWidget(self.terminal_input, stretch=1)
        terminal_command_layout.addWidget(self.terminal_run_btn)
        terminal_command_layout.addWidget(self.terminal_stop_btn)

        group_scripts_layout.addWidget(btn_select_dir)
        group_scripts_layout.addWidget(btn_scripts_help)
        group_scripts_layout.addWidget(self.scripts_dir_label)
        group_scripts_layout.addLayout(self.scripts_container)
        group_scripts_layout.addSpacing(8)
        group_scripts_layout.addLayout(terminal_header)
        group_scripts_layout.addWidget(self.terminal_cwd_label)
        group_scripts_layout.addWidget(self.terminal_output, stretch=1)
        group_scripts_layout.addLayout(terminal_command_layout)

        right_layout.addWidget(group_scripts, stretch=1)

        # ============================================================
        # FINAL LAYOUT: LEFT | MAP | RIGHT
        # ============================================================

        layout.addWidget(left_panel)
        layout.addWidget(canvas_container, stretch=1)
        layout.addWidget(right_panel)

        self.setCentralWidget(central)

        self._setup_status_bar()

        if hasattr(self, "_update_recent_files_combo"):
            self._update_recent_files_combo()

        default_scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_scripts")
        if os.path.isdir(default_scripts_dir):
            self._load_scripts_directory(default_scripts_dir)

        self._status_timer = QtCore.QTimer(self)
        self._status_timer.timeout.connect(self._update_status_bar)
        self._status_timer.start(1000)

    def _build_toolbar(self):
        toolbar = QtWidgets.QToolBar("Main")
        toolbar.setIconSize(QtCore.QSize(18, 18))
        self.addToolBar(toolbar)

        style = self.style()
        toolbar.addAction(
            style.standardIcon(QtWidgets.QStyle.SP_DialogOpenButton),
            "Open",
            self._open_gpx_dialog,
        )
        toolbar.addAction(
            style.standardIcon(QtWidgets.QStyle.SP_DialogSaveButton),
            "Save",
            self.editor.save_gpx,
        )
        toolbar.addSeparator()

        toolbar.addAction(
            style.standardIcon(QtWidgets.QStyle.SP_ArrowBack),
            "Undo",
            self.editor._undo_action,
        )
        toolbar.addAction(
            style.standardIcon(QtWidgets.QStyle.SP_ArrowForward),
            "Redo",
            self.editor._redo_action,
        )
        toolbar.addSeparator()
        toolbar.addAction(
            style.standardIcon(QtWidgets.QStyle.SP_DriveNetIcon),
            "Map ON/OFF",
            self.editor.toggle_basemap,
        )
        toolbar.addAction(
            style.standardIcon(QtWidgets.QStyle.SP_BrowserReload),
            "Reset view",
            self.editor._reset_view,
        )

    def _setup_status_bar(self):
        self._status_label = QtWidgets.QLabel()
        self.statusBar().addPermanentWidget(self._status_label, 1)
        self._update_status_bar()

    def _update_status_bar(self):
        total_points = int(self.editor.x.size)
        selected_points = len(self.editor.selected)
        duration_text = self.editor.format_duration(self.editor.get_track_duration())
        view_text = "Frozen" if self.editor.freeze_view else "Auto"
        shortcuts = "Scroll=zoom | RMB=pan | Del=delete | Ctrl+Z/Y=undo/redo | R=reset | M=map"
        self._status_label.setText(
            f"Mode: {self._selection_mode} | Points: {total_points} | "
            f"Selected: {selected_points} | Time: {duration_text} | View: {view_text} | {shortcuts}"
        )

    def _set_selection_mode(self, mode):
        self._selection_mode = mode
        if mode == "Rectangle":
            self.editor.activate_rectangle_selection()
        elif mode == "Lasso":
            self.editor.activate_lasso_selection()
        else:
            self.editor._deactivate_selectors()
        self._update_status_bar()

    def closeEvent(self, event):
        if self._print_listener is not None:
            remove_print_listener(self._print_listener)
            self._print_listener = None
        if self.terminal_process and self.terminal_process.state() != QtCore.QProcess.NotRunning:
            self.terminal_process.kill()
            self.terminal_process.waitForFinished(1000)
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1 and urls[0].isLocalFile() and urls[0].toLocalFile().lower().endswith(".gpx"):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            event.ignore()
            return

        path = urls[0].toLocalFile()
        if path.lower().endswith(".gpx"):
            if self.editor.load_gpx_from_path(path):
                self._update_recent_files_combo()
                self._update_status_bar()
                print(f"✅ Dropped and loaded: {path}")
            event.acceptProposedAction()
        else:
            event.ignore()

    def _set_freeze_view(self, state):
        self.editor.set_freeze_view(state)
        if hasattr(self, "freeze_view_button"):
            self.freeze_view_button.setText(f"Freeze view: {'ON' if self.editor.freeze_view else 'OFF'}")
        print(f"Freeze view: {'ON' if self.editor.freeze_view else 'OFF'}")
        self._update_status_bar()

    def _set_pick_tolerance(self, value):
        self.editor.set_pick_tolerance(value)
        print(f"Point pick radius: {self.editor.pick_tolerance_m:.1f} m")

    def _clear_selection(self):
        self.editor.clear_selection()
        self._update_status_bar()

    def _append_terminal_log(self, text):
        if not hasattr(self, "terminal_output"):
            return
        text = str(text).rstrip("\r\n")
        if not text:
            return
        self.terminal_output.appendPlainText(text)
        scrollbar = self.terminal_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _clear_terminal_log(self):
        self.terminal_output.clear()

    def _update_terminal_cwd_label(self):
        if hasattr(self, "terminal_cwd_label"):
            self.terminal_cwd_label.setText(self.terminal_cwd)
            self.terminal_cwd_label.setToolTip(self.terminal_cwd)

    def _set_terminal_running(self, running):
        self.terminal_input.setEnabled(not running)
        self.terminal_run_btn.setEnabled(not running)
        self.terminal_stop_btn.setEnabled(running)

    def _terminal_environment(self):
        env = QtCore.QProcessEnvironment.systemEnvironment()
        if os.name == "nt":
            venv_bin = os.path.join(self.project_dir, "venv", "Scripts")
        else:
            venv_bin = os.path.join(self.project_dir, "venv", "bin")
        if os.path.isdir(venv_bin):
            env.insert("PATH", venv_bin + os.pathsep + env.value("PATH"))
            env.insert("VIRTUAL_ENV", os.path.join(self.project_dir, "venv"))
        return env

    def _handle_terminal_cd(self, command):
        stripped = command.strip()
        lower = stripped.lower()
        target = None

        if lower in {"pwd", "get-location"}:
            self._append_terminal_log(self.terminal_cwd)
            return True
        if lower in {"cd", "sl", "set-location"}:
            self._append_terminal_log(self.terminal_cwd)
            return True
        if lower.startswith("cd "):
            target = stripped[3:].strip()
        elif lower.startswith("sl "):
            target = stripped[3:].strip()
        elif lower.startswith("set-location "):
            target = stripped[len("set-location "):].strip()
        else:
            return False

        target = target.strip().strip("\"'")
        target = os.path.expanduser(os.path.expandvars(target))
        if not os.path.isabs(target):
            target = os.path.abspath(os.path.join(self.terminal_cwd, target))

        if os.path.isdir(target):
            self.terminal_cwd = target
            self._update_terminal_cwd_label()
            self._append_terminal_log(self.terminal_cwd)
        else:
            self._append_terminal_log(f"Path not found: {target}")
        return True

    def _run_terminal_command(self):
        command = self.terminal_input.text().strip()
        if not command:
            return
        if self.terminal_process and self.terminal_process.state() != QtCore.QProcess.NotRunning:
            self._append_terminal_log("A command is already running. Stop it before starting another one.")
            return

        self.terminal_input.clear()
        prompt = f"PS {self.terminal_cwd}> {command}"

        if command.lower() in {"clear", "cls"}:
            self.terminal_output.clear()
            self._append_terminal_log(prompt)
            return

        self._append_terminal_log(prompt)
        if self._handle_terminal_cd(command):
            return

        process = QtCore.QProcess(self)
        self.terminal_process = process
        process.setWorkingDirectory(self.terminal_cwd)
        process.setProcessEnvironment(self._terminal_environment())
        process.setProcessChannelMode(QtCore.QProcess.SeparateChannels)
        process.readyReadStandardOutput.connect(
            lambda process=process: self._append_process_output(process.readAllStandardOutput())
        )
        process.readyReadStandardError.connect(
            lambda process=process: self._append_process_output(process.readAllStandardError())
        )
        process.finished.connect(
            lambda exit_code, exit_status, process=process: self._terminal_command_finished(
                process, exit_code, exit_status
            )
        )
        process.errorOccurred.connect(
            lambda error, process=process: self._terminal_command_error(process, error)
        )

        if os.name == "nt":
            program = "powershell.exe"
            args = [
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                + command,
            ]
        else:
            program = "pwsh"
            args = [
                "-NoLogo",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                + command,
            ]

        self._set_terminal_running(True)
        process.start(program, args)
        if not process.waitForStarted(1000):
            self._append_terminal_log(f"Could not start PowerShell: {process.errorString()}")
            self.terminal_process = None
            self._set_terminal_running(False)

    def _append_process_output(self, data):
        text = bytes(data).decode("utf-8", errors="replace").rstrip("\r\n")
        if text:
            self._append_terminal_log(text)

    def _terminal_command_finished(self, process, exit_code, exit_status):
        self._append_process_output(process.readAllStandardOutput())
        self._append_process_output(process.readAllStandardError())
        if exit_code != 0:
            self._append_terminal_log(f"[exit {exit_code}]")
        if self.terminal_process is process:
            self.terminal_process = None
        self._set_terminal_running(False)

    def _terminal_command_error(self, process, error):
        if self.terminal_process is process:
            self._append_terminal_log(f"PowerShell error: {process.errorString()}")
            self.terminal_process = None
            self._set_terminal_running(False)

    def _stop_terminal_command(self):
        if self.terminal_process and self.terminal_process.state() != QtCore.QProcess.NotRunning:
            self._append_terminal_log("[stopping process]")
            self.terminal_process.kill()
            self.terminal_process.waitForFinished(1000)

    def _open_gpx_dialog(self):
        if self.editor.load_gpx():
            self._update_recent_files_combo()
            self._update_status_bar()

    def _refresh_current_gpx(self):
        if self.editor.reload_current_gpx():
            self._update_recent_files_combo()
            self._update_status_bar()

    def _select_scripts_directory(self):
        default_dir = self.scripts_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_scripts")
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select scripts directory",
            default_dir,
        )
        if path:
            self._load_scripts_directory(path)

    def _load_scripts_directory(self, path):
        path = os.path.abspath(path)
        self.scripts_dir = path
        self.scripts_dir_label.setText(f"Folder:\n{path}")
        self.scripts_dir_label.setToolTip(path)
        self._clear_scripts_container()

        if not os.path.isdir(path):
            self.scripts_container.addWidget(QtWidgets.QLabel("Folder does not exist."))
            return

        script_paths = [
            os.path.join(path, name)
            for name in sorted(os.listdir(path), key=str.lower)
            if (
                name.lower().endswith(".py")
                and not name.startswith("_")
                and self._is_editor_script(os.path.join(path, name))
            )
        ]

        if not script_paths:
            self.scripts_container.addWidget(QtWidgets.QLabel("No .py scripts found."))
            return

        for script_path in script_paths:
            label = os.path.splitext(os.path.basename(script_path))[0].replace("_", " ")
            btn = QtWidgets.QPushButton(label)
            btn.setToolTip(script_path)
            btn.clicked.connect(lambda checked=False, p=script_path: self._run_custom_script(p))
            self.scripts_container.addWidget(btn)

    def _is_editor_script(self, script_path):
        try:
            with open(script_path, "r", encoding="utf-8-sig", errors="replace") as f:
                return any(line.lstrip().startswith("def run(") for line in f)
        except OSError:
            return False

    def _clear_scripts_container(self):
        while self.scripts_container.count():
            item = self.scripts_container.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child_layout = item.layout()
            if child_layout is not None:
                while child_layout.count():
                    child_item = child_layout.takeAt(0)
                    child_widget = child_item.widget()
                    if child_widget is not None:
                        child_widget.deleteLater()

    def _run_custom_script(self, script_path):
        if not self.editor.gpx_loaded or self.editor.gpx is None:
            QtWidgets.QMessageBox.warning(self, "No GPX loaded", "Open a GPX file before running a script.")
            return

        if not os.path.isfile(script_path):
            QtWidgets.QMessageBox.warning(self, "Script missing", f"Script file does not exist:\n{script_path}")
            self._load_scripts_directory(self.scripts_dir)
            return

        old_gpx = copy.deepcopy(self.editor.gpx)
        old_undo_len = len(self.editor._undo)
        script_name = os.path.basename(script_path)
        previous_print = builtins.print
        self._append_terminal_log(f"Running script: {script_name}")

        try:
            builtins.print = safe_print
            self.editor._push_undo()
            module_name = f"gpx_custom_script_{abs(hash(script_path))}"
            spec = importlib.util.spec_from_file_location(module_name, script_path)
            if spec is None or spec.loader is None:
                raise RuntimeError("Could not load script module.")

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            run_func = getattr(module, "run", None)
            if not callable(run_func):
                raise RuntimeError("Script must define a callable run(gpx) function.")

            result = self._call_custom_script(run_func)
            if result is not None and hasattr(result, "tracks"):
                self.editor.gpx = result

            if not self.editor.sync_from_loaded_gpx(reset_history=False, reset_view=True):
                raise RuntimeError("Script left the GPX without a valid track segment.")

        except Exception as exc:
            self.editor.gpx = old_gpx
            self.editor.sync_from_loaded_gpx(reset_history=False, reset_view=False)
            del self.editor._undo[old_undo_len:]
            details = traceback.format_exc()
            self._append_terminal_log(f"Script failed: {script_name}\n{details}")
            QtWidgets.QMessageBox.critical(
                self,
                "Script failed",
                f"{script_name} failed:\n\n{exc}\n\nDetails:\n{details}",
            )
            return
        finally:
            builtins.print = previous_print

        self._update_status_bar()
        self.statusBar().showMessage(f"Script completed: {script_name}", 5000)
        print(f"✅ Script completed: {script_path}")

    def _call_custom_script(self, run_func):
        context = SimpleNamespace(
            gpx=self.editor.gpx,
            editor=self.editor,
            selected_indices=sorted(self.editor.selected),
            current_path=self.editor.current_path,
            numpy=np,
            gpxpy=gpxpy,
        )
        params = list(inspect.signature(run_func).parameters.values())
        if not params:
            return run_func()
        if len(params) == 1:
            name = params[0].name.lower()
            if name in {"context", "ctx"}:
                return run_func(context)
            return run_func(self.editor.gpx)
        return run_func(self.editor.gpx, context)

    def _show_custom_scripts_help(self):
        QtWidgets.QMessageBox.information(
            self,
            "Custom Scripts API",
            (
                "Custom scripts are normal .py files in the selected folder.\n\n"
                "Basic shape:\n"
                "def run(gpx):\n"
                "    for track in gpx.tracks:\n"
                "        for segment in track.segments:\n"
                "            segment.points = segment.points[::2]\n\n"
                "Mutate the passed gpx object in place. After run() returns, GPXEditor rebuilds "
                "the current view from that object.\n\n"
                "Optional advanced signature:\n"
                "def run(gpx, context):\n"
                "    # context.editor, context.selected_indices, context.current_path\n"
                "    pass\n\n"
                "A script can also use def run(context) if it only needs the context object."
            ),
        )

    def _open_recent_file(self, index):
        if index < 0 or index >= self.recent_files_combo.count():
            return
        path = self.recent_files_combo.itemData(index)
        if not path:
            return
        if self.editor.load_gpx_from_path(path):
            self._update_recent_files_combo()
            self._update_status_bar()

    def _update_recent_files_combo(self):
        self.recent_files_combo.blockSignals(True)
        self.recent_files_combo.clear()

        if not self.editor.recent_files:
            self.recent_files_combo.addItem("No recent files")
            self.recent_files_combo.setEnabled(False)
        else:
            for path in self.editor.recent_files:
                label = os.path.basename(path) or path
                self.recent_files_combo.addItem(label, path)
            self.recent_files_combo.setEnabled(True)

        self.recent_files_combo.blockSignals(False)


@atexit.register
def cleanup():
    try:
        os.remove("temp_map.html")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("GPXEditor")
    if os.path.exists(APP_ICON_PATH):
        app.setWindowIcon(QtGui.QIcon(APP_ICON_PATH))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
