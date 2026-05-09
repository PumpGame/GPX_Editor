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
import webbrowser

import numpy as np
import gpxpy
import gpxpy.gpx
import folium
import contextily as ctx
from pyproj import Transformer

from PySide6 import QtCore, QtWidgets

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.widgets import RectangleSelector, LassoSelector
from matplotlib.backend_bases import MouseButton
from matplotlib.path import Path

try:
    from scipy.spatial import cKDTree as KDTree
except Exception:
    KDTree = None


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

        # --- podkład mapowy ---
        self.basemap_enabled = True
        self.basemap_loaded = False
        self.basemap_img = None
        self.basemap_extent = None
        self.basemap_artist = None

        # --- widok / interakcja ---
        self.xlim_current = None
        self.ylim_current = None
        self.dragged = False
        self.drag_origin = None
        self.last_canvas_xy = None
        self.pending_drag = False
        self.press_idx = None
        self.press_key = None
        self.press_canvas_xy = None
        self.press_on_selected = False
        self.hover_idx = None
        self.hover_annotation = None
        self.track_line = None
        self.scatter = None

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

    # -------------------------- GPX I/O --------------------------

    def load_gpx(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            None,
            "Open GPX",
            "",
            "GPX files (*.gpx)",
        )
        if not path:
            return

        with open(path, 'r', encoding='utf-8') as f:
            self.gpx = gpxpy.parse(f)

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
            return

        self.track = best_track
        self.segment = best_seg

        lons = [p.longitude for p in self.segment.points]
        lats = [p.latitude for p in self.segment.points]
        X, Y = self.to_merc.transform(lons, lats)
        self.x = np.asarray(X, dtype=float)
        self.y = np.asarray(Y, dtype=float)
        self.point_metadata = list(self.segment.points)

        self.selected.clear()
        self._redo.clear()
        self._undo.clear()
        self.gpx_loaded = True
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None

        # reset podkładu/widoku
        self.basemap_loaded = False
        self.basemap_img = None
        self.basemap_extent = None
        self.xlim_current = None
        self.ylim_current = None

        self._update_plot(full=True)
        self._update_info_text()
        print(f"✅ Loaded: {path} | points: {len(self.x)}")

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
            x0, x1 = float(self.x.min() - pad), float(self.x.max() + pad)
            y0, y1 = float(self.y.min() - pad), float(self.y.max() + pad)
            self.basemap_img, self.basemap_extent = ctx.bounds2img(
                x0, y0, x1, y1, zoom=17, source=ctx.providers.OpenStreetMap.Mapnik
            )
            self.basemap_loaded = True
        except Exception as e:
            print("❌ Failed to load map:", e)
            self.basemap_enabled = False

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
        self.selected = set(sel)
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()

    def get_track_duration(self):
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
            self._deactivate_selectors()
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
        self._deactivate_selectors()

    # ---- lasso ----
    def activate_lasso_selection(self):
        self._deactivate_selectors()
        self.lasso_selector = LassoSelector(self.ax, onselect=self._on_lasso_select)
        print("✏️ Mode: lasso selection (draw around points)")

    def _on_lasso_select(self, verts):
        if not verts:
            self._deactivate_selectors()
            return
        # verts są w koord. ekranu — przelicz do danych
        poly_data = self.ax.transData.inverted().transform(verts)
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
        self._deactivate_selectors()

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
        self.selected.clear()
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()
        print(f"🗑️ Removed {removed} points")

    # -------------------------- Zdarzenia --------------------------

    def _on_press(self, event):
        if not self.gpx_loaded or event.xdata is None or event.ydata is None:
            return

        # PPM – pan
        if event.button == MouseButton.RIGHT:
            self.last_canvas_xy = (event.x, event.y)
            return

        # LPM – wybór/przeciąganie
        if event.button == MouseButton.LEFT:
            idx, dist = self._nearest_index(event.xdata, event.ydata)
            if idx is None:
                return
            pick_tol = 12.0  # ~metry w EPSG:3857
            if dist is None or dist > pick_tol:
                # klik w puste: wyczyść selekcję (chyba że trzymasz Shift/Ctrl)
                if not (event.key == 'shift' or event.key == 'control'):
                    self.selected.clear()
                    self._update_plot()
                    self._update_info_text()
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
            self._update_plot()
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
            self.fig.canvas.draw_idle()
            return

        if not self.dragged and event.xdata is not None and event.ydata is not None:
            idx, dist = self._nearest_index(event.xdata, event.ydata)
            pick_tol = 12.0  # ~metry w EPSG:3857
            new_hover = idx if (dist is not None and dist <= pick_tol) else None
            if new_hover != self.hover_idx:
                self.hover_idx = new_hover
                self._update_plot()

        if self.pending_drag and not self.dragged and event.x is not None and event.y is not None:
            dx_px = event.x - self.press_canvas_xy[0]
            dy_px = event.y - self.press_canvas_xy[1]
            if (dx_px * dx_px + dy_px * dy_px) >= 16:  # 4px próg
                self._apply_selection_click(self.press_idx, self.press_key)
                if self.press_idx in self.selected:
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
        self.fig.canvas.draw_idle()

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

    def _reset_view(self):
        if self.x.size:
            self.xlim_current = (float(self.x.min()) - 500, float(self.x.max()) + 500)
            self.ylim_current = (float(self.y.min()) - 500, float(self.y.max()) + 500)
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

        # Podkład
        if self.basemap_enabled:
            self._ensure_basemap()
            if self.basemap_img is not None and self.basemap_extent is not None:
                if self.basemap_artist is None:
                    self.basemap_artist = self.ax.imshow(
                        self.basemap_img,
                        extent=self.basemap_extent,
                        interpolation='bilinear',
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
            colors = np.array(['red'] * self.x.size, dtype=object)
            if self.selected:
                idxs = np.fromiter(self.selected, dtype=int)
                idxs = idxs[(idxs >= 0) & (idxs < self.x.size)]
                colors[idxs] = 'green'
            if self.hover_idx is not None and 0 <= self.hover_idx < self.x.size:
                if self.hover_idx not in self.selected:
                    colors[self.hover_idx] = 'orange'
            if self.track_line is None:
                (self.track_line,) = self.ax.plot(self.x, self.y, '-', zorder=4)
            else:
                self.track_line.set_data(self.x, self.y)

            if self.scatter is None:
                self.scatter = self.ax.scatter(self.x, self.y, c=colors, s=14, zorder=5)
            else:
                offsets = np.column_stack((self.x, self.y))
                self.scatter.set_offsets(offsets)
                self.scatter.set_facecolor(colors)

            if self.xlim_current and self.ylim_current:
                self.ax.set_xlim(*self.xlim_current)
                self.ax.set_ylim(*self.ylim_current)
            else:
                self._reset_view()
                self.ax.relim()
                self.ax.autoscale_view()
                self.canvas.draw_idle()


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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GPXEditor — by surfplorer")
        self.resize(1560, 940)
        self._selection_mode = "Single point"
        self._apply_theme()
        self._build_ui()

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
                border-radius: 6px;
                padding: 6px 10px;
            }
            QPushButton:hover {
                background: #eef2f6;
            }
            QPushButton:pressed {
                background: #e1e6eb;
            }
            QLineEdit, QSpinBox {
                background: #ffffff;
                border: 1px solid #d6dbe1;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QRadioButton {
                spacing: 10px;
                padding: 8px 10px;
                border: 1px solid transparent;
                border-radius: 6px;
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
                width: 16px;
                height: 16px;
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
                border-radius: 8px;
                margin-top: 10px;
                padding: 6px;
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

        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_panel.setFixedWidth(230)

        group_file = QtWidgets.QGroupBox("File")
        group_file_layout = QtWidgets.QVBoxLayout(group_file)
        btn_open = QtWidgets.QPushButton("Open GPX")
        btn_open.clicked.connect(self.editor.load_gpx)
        btn_save = QtWidgets.QPushButton("Save…")
        btn_save.clicked.connect(self.editor.save_gpx)
        group_file_layout.addWidget(btn_open)
        group_file_layout.addWidget(btn_save)

        group_delete = QtWidgets.QGroupBox("Delete points")
        group_delete_layout = QtWidgets.QVBoxLayout(group_delete)
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

        group_selection = QtWidgets.QGroupBox("Selection mode")
        group_selection_layout = QtWidgets.QVBoxLayout(group_selection)
        radio_single = QtWidgets.QRadioButton("Single point")
        radio_rect = QtWidgets.QRadioButton("Rectangle")
        radio_lasso = QtWidgets.QRadioButton("Lasso")
        radio_single.setChecked(True)
        radio_single.toggled.connect(lambda checked: checked and self._set_selection_mode("Single point"))
        radio_rect.toggled.connect(lambda checked: checked and self._set_selection_mode("Rectangle"))
        radio_lasso.toggled.connect(lambda checked: checked and self._set_selection_mode("Lasso"))
        group_selection_layout.addWidget(radio_single)
        group_selection_layout.addWidget(radio_rect)
        group_selection_layout.addWidget(radio_lasso)

        group_selection_edit = QtWidgets.QGroupBox("Selection edit")
        group_selection_edit_layout = QtWidgets.QVBoxLayout(group_selection_edit)
        btn_delete_selected = QtWidgets.QPushButton("Delete selected")
        btn_delete_selected.clicked.connect(self.editor.delete_selected)
        btn_clear_selection = QtWidgets.QPushButton("Clear selection")
        btn_clear_selection.setEnabled(False)
        # TODO: implement clear selection action in the editor and wire this button.
        group_selection_edit_layout.addWidget(btn_delete_selected)
        group_selection_edit_layout.addWidget(btn_clear_selection)

        group_view = QtWidgets.QGroupBox("View / tools")
        group_view_layout = QtWidgets.QVBoxLayout(group_view)
        btn_reset_view = QtWidgets.QPushButton("Reset view")
        btn_reset_view.clicked.connect(self.editor._reset_view)
        btn_copy = QtWidgets.QPushButton("Copy coordinates")
        btn_copy.clicked.connect(self.editor.copy_selected_coords)
        btn_preview = QtWidgets.QPushButton("Open in browser")
        btn_preview.clicked.connect(lambda: self.editor.show_map())
        group_view_layout.addWidget(btn_reset_view)
        group_view_layout.addWidget(btn_copy)
        group_view_layout.addWidget(btn_preview)

        left_layout.addWidget(group_file)
        left_layout.addWidget(group_delete)
        left_layout.addWidget(group_selection)
        left_layout.addWidget(group_selection_edit)
        left_layout.addWidget(group_view)
        left_layout.addStretch()

        self.editor.cut_input = cut_input

        canvas_container = QtWidgets.QWidget()
        canvas_layout = QtWidgets.QVBoxLayout(canvas_container)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.addWidget(canvas, stretch=1)

        layout.addWidget(left_panel)
        layout.addWidget(canvas_container, stretch=1)

        self.setCentralWidget(central)
        self._setup_status_bar()

        self._status_timer = QtCore.QTimer(self)
        self._status_timer.timeout.connect(self._update_status_bar)
        self._status_timer.start(300)

    def _build_toolbar(self):
        toolbar = QtWidgets.QToolBar("Main")
        toolbar.setIconSize(QtCore.QSize(18, 18))
        self.addToolBar(toolbar)

        style = self.style()
        toolbar.addAction(
            style.standardIcon(QtWidgets.QStyle.SP_DialogOpenButton),
            "Open",
            self.editor.load_gpx,
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
        shortcuts = "Scroll=zoom | RMB=pan | Del=delete | Ctrl+Z/Y=undo/redo | R=reset | M=map"
        self._status_label.setText(
            f"Mode: {self._selection_mode} | Points: {total_points} | "
            f"Selected: {selected_points} | Time: {duration_text} | {shortcuts}"
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


@atexit.register
def cleanup():
    try:
        os.remove("temp_map.html")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("GPXEditor")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
