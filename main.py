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
Tested on Python 3.10+ with TkAgg backend.
Dependencies: matplotlib, gpxpy, folium, contextily, pyproj, numpy (optional: scipy for KDTree)
"""
import matplotlib
matplotlib.use("TkAgg")

import os
import atexit
import webbrowser
from tkinter import Tk, filedialog

import numpy as np
import gpxpy
import gpxpy.gpx
import folium
import contextily as ctx
from pyproj import Transformer

import matplotlib.pyplot as plt
from matplotlib.widgets import Button, TextBox, RectangleSelector, LassoSelector
from matplotlib.backend_bases import MouseButton
from matplotlib.path import Path

try:
    from scipy.spatial import cKDTree as KDTree
except Exception:
    KDTree = None


class GPXEditor:
    def __init__(self):
        # --- dane GPX / stan ---
        self.x = np.array([], dtype=float)   # EPSG:3857
        self.y = np.array([], dtype=float)
        self.selected = set()
        self.gpx = None
        self.track = None
        self.segment = None
        self.gpx_loaded = False
        self.kdtree = None

        # --- podkład mapowy ---
        self.basemap_enabled = True
        self.basemap_loaded = False
        self.basemap_img = None
        self.basemap_extent = None

        # --- widok / interakcja ---
        self.xlim_current = None
        self.ylim_current = None
        self.dragged = False
        self.drag_origin = None
        self.last_canvas_xy = None

        # --- undo/redo ---
        self._undo = []
        self._redo = []
        self._undo_limit = 100

        # --- transformacje ---
        self.to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        self.to_wgs84 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

        # --- Tk root (dialogs + clipboard) ---
        self.tk = Tk()
        self.tk.withdraw()

        # --- GUI ---
        self.ui_theme = {
            "window_bg": "#f3f5f7",
            "plot_bg": "#ffffff",
            "panel_bg": "#e9edf2",
            "text": "#1f2933",
            "muted_text": "#5f6b7a",
            "primary": "#2563eb",
            "primary_hover": "#1d4ed8",
            "success": "#059669",
            "success_hover": "#047857",
            "warning": "#d97706",
            "warning_hover": "#b45309",
            "neutral": "#6b7280",
            "neutral_hover": "#4b5563",
        }
        self.fig, self.ax = plt.subplots(figsize=(14, 9))
        self.fig.patch.set_facecolor(self.ui_theme["window_bg"])
        self.ax.set_facecolor(self.ui_theme["plot_bg"])
        plt.subplots_adjust(bottom=0.24)
        self.info_text = self.ax.text(
            0.01, 0.98, '', transform=self.ax.transAxes, va='top',
            fontsize=10, color=self.ui_theme["text"],
            bbox=dict(
                facecolor=self.ui_theme["panel_bg"],
                edgecolor="#d0d7de",
                boxstyle="round,pad=0.3",
                alpha=0.9,
            ),
        )
        self._connect_events()
        self._build_toolbar()
        self._update_plot(full=True)
        self._set_title()
        # --- podpis autora ---
        ax_author = plt.axes([0.83, 0.01, 0.15, 0.03])  # pozycja (x, y, szerokość, wysokość)
        ax_author.axis("off")
        ax_author.text(0.5, 0.5, "© 2025 surfplorer",
                    ha="center", va="center", fontsize=8, color="gray")

        plt.show()

    # -------------------------- GUI helpers --------------------------

    def _set_title(self):
        try:
            self.fig.canvas.manager.set_window_title("GPXEditor — by surfplorer")
        except Exception:
            pass
        self.ax.set_title(
            "Scroll=zoom | PPM=pan | LPM=wybór/przeciąganie | Shift+klik=toggle | "
            "Ctrl+klik=zakres | Del=usuń | Ctrl+Z/Y=undo/redo | R=reset | M=mapa | "
            "[ / ]: -/+ 10 | Shift+[ / ]: 50 | Ctrl+[ / ]: 100"
        )

    def _connect_events(self):
        c = self.fig.canvas.mpl_connect
        c('button_press_event', self._on_press)
        c('motion_notify_event', self._on_motion)
        c('button_release_event', self._on_release)
        c('scroll_event', self._on_scroll)
        c('key_press_event', self._on_key)

    def _build_toolbar(self):
        # Dwie rzędy kontrolerów (mniejsze przyciski)
        h = 0.05
        y1 = 0.02  # dolny rząd
        y2 = 0.09  # górny rząd

        def axat(x, y, w):
            return plt.axes([x, y, w, h])

        def style_button(button, color, hover, text_color=None, font_size=9):
            button.color = color
            button.hovercolor = hover
            if text_color is None:
                text_color = "white"
            button.label.set_color(text_color)
            button.label.set_fontsize(font_size)

        # --- Górny rząd ---
        self.ax_open = axat(0.02, y2, 0.10)
        self.ax_save = axat(0.13, y2, 0.10)
        self.ax_map_toggle = axat(0.24, y2, 0.11)
        self.ax_map_browser = axat(0.36, y2, 0.14)
        self.ax_copy = axat(0.51, y2, 0.14)
        self.ax_undo = axat(0.66, y2, 0.08)
        self.ax_redo = axat(0.75, y2, 0.08)

        self.btn_open = Button(self.ax_open, 'Otwórz GPX')
        self.btn_open.on_clicked(lambda e: self.load_gpx())
        style_button(self.btn_open, self.ui_theme["primary"], self.ui_theme["primary_hover"])

        self.btn_save = Button(self.ax_save, 'Zapisz…')
        self.btn_save.on_clicked(lambda e: self.save_gpx())
        style_button(self.btn_save, self.ui_theme["success"], self.ui_theme["success_hover"])

        self.btn_map = Button(self.ax_map_toggle, 'Mapa ON/OFF')
        self.btn_map.on_clicked(lambda e: self.toggle_basemap())
        style_button(self.btn_map, self.ui_theme["neutral"], self.ui_theme["neutral_hover"])

        self.btn_show = Button(self.ax_map_browser, 'Podgląd w przeglądarce')
        self.btn_show.on_clicked(lambda e: self.show_map())
        style_button(self.btn_show, self.ui_theme["primary"], self.ui_theme["primary_hover"])

        self.btn_copy = Button(self.ax_copy, '✎ Kopiuj współrzędne')
        self.btn_copy.on_clicked(lambda e: self.copy_selected_coords())
        style_button(self.btn_copy, self.ui_theme["success"], self.ui_theme["success_hover"])

        self.btn_undo = Button(self.ax_undo, 'Undo')
        self.btn_undo.on_clicked(lambda e: self._undo_action())
        style_button(self.btn_undo, self.ui_theme["neutral"], self.ui_theme["neutral_hover"])

        self.btn_redo = Button(self.ax_redo, 'Redo')
        self.btn_redo.on_clicked(lambda e: self._redo_action())
        style_button(self.btn_redo, self.ui_theme["neutral"], self.ui_theme["neutral_hover"])

        # --- Dolny rząd ---
        self.ax_cut1 = axat(0.02, y1, 0.10)
        self.ax_cut2 = axat(0.13, y1, 0.10)
        self.ax_txt = axat(0.24, y1, 0.06)  # TextBox na X
        self.ax_cutX1 = axat(0.31, y1, 0.12)
        self.ax_cutX2 = axat(0.44, y1, 0.12)
        self.ax_rect = axat(0.57, y1, 0.16)
        self.ax_lasso = axat(0.74, y1, 0.16)

        self.btn_cut_start = Button(self.ax_cut1, '< Usuń 1 start')
        self.btn_cut_start.on_clicked(lambda e: self.remove_first_n(1))
        style_button(self.btn_cut_start, self.ui_theme["warning"], self.ui_theme["warning_hover"])

        self.btn_cut_end = Button(self.ax_cut2, 'Usuń 1 koniec >')
        self.btn_cut_end.on_clicked(lambda e: self.remove_last_n(1))
        style_button(self.btn_cut_end, self.ui_theme["warning"], self.ui_theme["warning_hover"])

        self.txt_cut = TextBox(self.ax_txt, 'X:', initial='10')
        self.txt_cut.ax.set_facecolor(self.ui_theme["panel_bg"])
        self.txt_cut.text_disp.set_color(self.ui_theme["text"])
        self.txt_cut.text_disp.set_fontsize(9)
        self.txt_cut.label.set_color(self.ui_theme["muted_text"])
        self.txt_cut.label.set_fontsize(9)
        self.btn_cutX_start = Button(self.ax_cutX1, f'Usuń X start')
        self.btn_cutX_start.on_clicked(lambda e: self._remove_x_from('start'))
        style_button(self.btn_cutX_start, self.ui_theme["warning"], self.ui_theme["warning_hover"])

        self.btn_cutX_end = Button(self.ax_cutX2, f'Usuń X koniec')
        self.btn_cutX_end.on_clicked(lambda e: self._remove_x_from('end'))
        style_button(self.btn_cutX_end, self.ui_theme["warning"], self.ui_theme["warning_hover"])

        self.btn_rect = Button(self.ax_rect, '■ Zaznacz prostokątem')
        self.btn_rect.on_clicked(lambda e: self.activate_rectangle_selection())
        style_button(self.btn_rect, self.ui_theme["primary"], self.ui_theme["primary_hover"])

        self.btn_lasso = Button(self.ax_lasso, '✏ Zaznacz lassem')
        self.btn_lasso.on_clicked(lambda e: self.activate_lasso_selection())
        style_button(self.btn_lasso, self.ui_theme["primary"], self.ui_theme["primary_hover"])

        # Selektory (inaczej nie pojawią się atrybuty przy pierwszym użyciu)
        self.rect_selector = None
        self.lasso_selector = None

    # -------------------------- GPX I/O --------------------------

    def load_gpx(self):
        path = filedialog.askopenfilename(title="Otwórz GPX", filetypes=[("GPX files", "*.gpx")])
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
            print("❌ GPX nie zawiera odpowiedniego segmentu.")
            return

        self.track = best_track
        self.segment = best_seg

        lons = [p.longitude for p in self.segment.points]
        lats = [p.latitude for p in self.segment.points]
        X, Y = self.to_merc.transform(lons, lats)
        self.x = np.asarray(X, dtype=float)
        self.y = np.asarray(Y, dtype=float)

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
        print(f"✅ Załadowano: {path} | punkty: {len(self.x)}")

    def save_gpx(self):
        if not self.gpx_loaded:
            print("⚠️ Najpierw otwórz plik GPX")
            return

        lons, lats = self.to_wgs84.transform(self.x, self.y)

        new_points = []
        for i, (lon, lat) in enumerate(zip(lons, lats)):
            if i < len(self.segment.points):
                p = self.segment.points[i]
                new_points.append(gpxpy.gpx.GPXTrackPoint(
                    latitude=lat, longitude=lon,
                    elevation=p.elevation, time=p.time, symbol=p.symbol,
                    comment=p.comment, name=p.name
                ))
            else:
                new_points.append(gpxpy.gpx.GPXTrackPoint(latitude=lat, longitude=lon))

        self.segment.points = new_points

        path = filedialog.asksaveasfilename(
            title="Zapisz jako...",
            defaultextension=".gpx",
            initialfile="edited.gpx",
            filetypes=[("GPX files", "*.gpx")]
        )
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.gpx.to_xml())
            print("💾 Zapisano jako:", path)
        else:
            print("❌ Zapis anulowany.")

    # -------------------------- Podkład / folium --------------------------

    def toggle_basemap(self):
        self.basemap_enabled = not self.basemap_enabled
        self._update_plot(full=True)
        print(f"🗺️ Podkład: {'ON' if self.basemap_enabled else 'OFF'}")

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
            print("❌ Błąd ładowania mapy:", e)
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
        folium.Marker(points[-1], tooltip="Koniec").add_to(m)
        m.save(html_path)
        webbrowser.open("file://" + os.path.abspath(html_path), new=0)

    # -------------------------- Edycja / selekcja --------------------------

    def _push_undo(self):
        if self.x.size == 0:
            return
        self._undo.append((self.x.copy(), self.y.copy(), tuple(sorted(self.selected))))
        if len(self._undo) > self._undo_limit:
            self._undo.pop(0)
        self._redo.clear()

    def _undo_action(self):
        if not self._undo:
            return
        self._redo.append((self.x.copy(), self.y.copy(), tuple(sorted(self.selected))))
        x, y, sel = self._undo.pop()
        self.x, self.y = x, y
        self.selected = set(sel)
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()

    def _redo_action(self):
        if not self._redo:
            return
        self._undo.append((self.x.copy(), self.y.copy(), tuple(sorted(self.selected))))
        x, y, sel = self._redo.pop()
        self.x, self.y = x, y
        self.selected = set(sel)
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()

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
        print("🔲 Tryb: prostokąt (kliknij i przeciągnij)")

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
            print(f"🔲 Zaznaczono {len(idxs)} punktów (prostokąt)")
            self._update_plot()
            self._update_info_text()
        self._deactivate_selectors()

    # ---- lasso ----
    def activate_lasso_selection(self):
        self._deactivate_selectors()
        self.lasso_selector = LassoSelector(self.ax, onselect=self._on_lasso_select)
        print("✏️ Tryb: lasso (rysuj kształt dookoła punktów)")

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
            print(f"✏️ Zaznaczono {len(idxs)} punktów (lasso)")
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
            print("ℹ️ Brak zaznaczenia.")
            return
        lines = []
        for idx in sorted(self.selected):
            if 0 <= idx < self.x.size:
                lon, lat = self.to_wgs84.transform(self.x[idx], self.y[idx])
                lines.append(f"{lat:.7f}, {lon:.7f}")
        text = "\n".join(lines)
        try:
            self.tk.clipboard_clear()
            self.tk.clipboard_append(text)
            self.tk.update()  # aby schowek nie znikał po zamknięciu
            print("📋 Skopiowano do schowka:")
            print(text)
        except Exception as e:
            print("❌ Nie udało się skopiować do schowka:", e)

    # ---- przycinanie ----
    def _remove_x_from(self, where):
        value = self.txt_cut.text.strip()
        try:
            n = int(value)
        except Exception:
            print("⚠️ Podaj liczbę całkowitą w polu X.")
            return
        if n <= 0:
            print("⚠️ X musi być > 0.")
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
        # przesuń selekcję
        self.selected = {i - n for i in self.selected if i - n >= 0}
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()
        print(f"🔻 Usunięto pierwsze {n} punktów")

    def remove_last_n(self, n):
        if self.x.size <= 1:
            return
        n = max(0, min(n, int(self.x.size)-1))
        if n == 0:
            return
        self._push_undo()
        self.x = self.x[:-n]
        self.y = self.y[:-n]
        self.selected = {i for i in self.selected if i < self.x.size}
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()
        print(f"🔺 Usunięto ostatnie {n} punktów")

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
        self.selected.clear()
        self.kdtree = KDTree(np.c_[self.x, self.y]) if KDTree is not None else None
        self._update_plot(full=True)
        self._update_info_text()
        print(f"🗑️ Usunięto {removed} punktów")

    # -------------------------- Zdarzenia --------------------------

    def _on_press(self, event):
        if not self.gpx_loaded or event.xdata is None or event.ydata is None:
            return

        # PPM – pan
        if event.button == MouseButton.RIGHT:
            self.last_canvas_xy = (event.xdata, event.ydata)
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
                return

            if event.key == 'shift':
                if idx in self.selected:
                    self.selected.remove(idx)
                else:
                    self.selected.add(idx)
                self._update_plot()
                self._update_info_text()
                return

            if event.key == 'control':
                if self.selected:
                    a = min(self.selected)
                    b = idx
                    lo, hi = (a, b) if a <= b else (b, a)
                    self.selected = set(range(lo, hi + 1))
                else:
                    self.selected = {idx}
                self._update_plot()
                self._update_info_text()
                return

            # zwykły klik – pojedynczy wybór i start przeciągania
            self.selected = {idx}
            self.dragged = True
            self.drag_origin = (event.xdata, event.ydata)
            self._push_undo()
            self._update_plot()
            self._update_info_text()

    def _on_motion(self, event):
        # pan PPM
        if event.button == MouseButton.RIGHT and self.last_canvas_xy and event.xdata and event.ydata:
            dx = event.xdata - self.last_canvas_xy[0]
            dy = event.ydata - self.last_canvas_xy[1]
            x0, x1 = self.ax.get_xlim()
            y0, y1 = self.ax.get_ylim()
            self.ax.set_xlim(x0 - dx, x1 - dx)
            self.ax.set_ylim(y0 - dy, y1 - dy)
            self.last_canvas_xy = (event.xdata, event.ydata)
            self.fig.canvas.draw_idle()
            return

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
        self.dragged = False
        self.drag_origin = None
        self.last_canvas_xy = None
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

        # Podkład
        if self.basemap_enabled:
            self._ensure_basemap()
            if self.basemap_img is not None and self.basemap_extent is not None:
                self.ax.imshow(self.basemap_img, extent=self.basemap_extent,
                               interpolation='bilinear', zorder=0)

        # Ślad
        if self.x.size:
            self.ax.plot(self.x, self.y, '-', zorder=4)
            colors = np.array(['red'] * self.x.size, dtype=object)
            if self.selected:
                idxs = np.fromiter(self.selected, dtype=int)
                idxs = idxs[(idxs >= 0) & (idxs < self.x.size)]
                colors[idxs] = 'green'
            self.ax.scatter(self.x, self.y, c=colors, s=14, zorder=5)

            if self.xlim_current and self.ylim_current:
                self.ax.set_xlim(*self.xlim_current)
                self.ax.set_ylim(*self.ylim_current)
            else:
                self._reset_view()

        # info box – odtwórz box po clear()
        self.info_text = self.ax.text(
            0.01, 0.98, self.info_text.get_text(),
            transform=self.ax.transAxes, va='top',
            fontsize=10, color='black', bbox=dict(facecolor='white', alpha=0.7)
        )
        self._set_title()
        self.fig.canvas.draw_idle()

    def _update_info_text(self):
        if not (self.gpx_loaded and self.selected and self.segment and self.segment.points):
            self.info_text.set_text("")
            self.fig.canvas.draw_idle()
            return

        lines = []
        for idx in sorted(self.selected):
            if 0 <= idx < len(self.segment.points):
                p = self.segment.points[idx]
                t = p.time.isoformat() if p.time else "—"
                ele = f"{p.elevation:.1f} m" if p.elevation is not None else "—"
                # lat/lon po aktualnej edycji
                lon, lat = self.to_wgs84.transform(self.x[idx], self.y[idx])
                lines.append(f"#{idx} — lat={lat:.7f}, lon={lon:.7f} | t={t} | ele={ele}")
        self.info_text.set_text("\n".join(lines))
        self.fig.canvas.draw_idle()


@atexit.register
def cleanup():
    try:
        os.remove("temp_map.html")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    GPXEditor()
