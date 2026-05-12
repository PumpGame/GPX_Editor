#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPX Synthetic Path Generator (v2.3)
Autor: ChatGPT (GSV project)
Data: 2025-08-22

Poprawki w v2.3:
- NAPRAWIONY regex liczbowy (poprzednio był podwójnie escapowany i nie łapał cyfr).
- Dodatkowe czyszczenie wejścia: NBSP (U+00A0), NNBSP (U+202F), FIGURE SPACE (U+2007),
  oraz znak minus (U+2212) -> zwykły '-'.
- Zachowane: auto-normalizacja do 8 miejsc (TRUNC), odporność na „śmietnik” w linii,
  wszystkie funkcje v2.2 (extensions, tryby czasu t/k, stała prędkość, poprawny zapis GPX).
"""

import math
import sys
import os
import re
from datetime import datetime, timedelta

def info(msg): print(f"[i] {msg}")
def ok(msg): print(f"[✓] {msg}")
def warn(msg): print(f"[!] {msg}")
def err(msg): print(f"[x] {msg}")

def wait_or_exit():
    try:
        input("\n[•] Wciśnij Enter, aby zakończyć...")
    except KeyboardInterrupt:
        pass

# ------------------------------ Parsowanie danych wejściowych ------------------------------

# Poprawny regex: raw string z JEDNYM backslashem przed d i .
FLOAT_EXTRACT = re.compile(r'[-+]?\d+(?:\.\d+)?')

def trunc_to_dec(x, n=8):
    factor = 10 ** n
    if x >= 0:
        return math.floor(x * factor) / factor
    else:
        return math.ceil(x * factor) / factor

def clean_line(line: str) -> str:
    if not line:
        return line
    # Zamień różne białe znaki na zwykłą spację
    line = (line
            .replace("\u00A0", " ")  # NBSP
            .replace("\u202F", " ")  # NNBSP
            .replace("\u2007", " ")  # FIGURE SPACE
            .replace("\u2212", "-")  # minus U+2212 -> ASCII '-'
           )
    return line.strip()

def parse_latlon(line):
    """
    Przyjmuje dowolną postać 'lat, lon' (lub z separatorami ; | spacja/tab).
    - Czyści NBSP, nietypowy minus itp.
    - Wyciąga pierwsze DWIE liczby zmiennoprzecinkowe z linii.
    - Jeśli >2 liczb – ostrzega i używa dwóch pierwszych.
    - Normalizuje do 8 miejsc po przecinku (TRUNC).
    """
    line = clean_line(line)
    if not line:
        raise ValueError("Pusta linia – wklej 'lat, lon'.")
    nums = FLOAT_EXTRACT.findall(line)
    if len(nums) < 2:
        raise ValueError("Nie znaleziono dwóch liczb w linii. Użyj np. 51.38669323, 23.27335323")
    if len(nums) > 2:
        warn("Wiersz zawiera >2 liczb – biorę DWIE pierwsze (pozostałe ignoruję).")
    lat = float(nums[0])
    lon = float(nums[1])
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("Zakres: lat ∈ [-90,90], lon ∈ [-180,180]")
    lat_n = trunc_to_dec(lat, 8)
    lon_n = trunc_to_dec(lon, 8)
    if lat_n != lat or lon_n != lon:
        warn(f"Znormalizowano do 8 miejsc po przecinku: lat={lat_n:.8f}, lon={lon_n:.8f}")
    return lat_n, lon_n

def parse_start_or_end_time(s):
    s = clean_line(s)
    if 't' in s and 'T' not in s:
        s = s.replace('t', 'T')
    if 'T' not in s:
        raise ValueError("Czas musi zawierać 'T' lub 't' – np. 2025-05-01T23:04:44.500")
    date_part, time_part = s.split('T', 1)
    if time_part.count(":") == 3 and "." not in time_part:
        hh, mm, ss, ms = time_part.split(":")
        time_part = f"{hh}:{mm}:{ss}.{ms}"
        s = f"{date_part}T{time_part}"
    if "." not in s:
        s = s + ".000"
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
    except Exception:
        raise ValueError("Niepoprawny format czasu. Użyj np. 2025-05-01T23:04:44.500")
    return dt

def parse_speed(s):
    s = clean_line(s).lower().replace(" ", "")
    if s.endswith("ms"):
        return float(s[:-2])
    if s.endswith("mps"):
        return float(s[:-3])
    if s.endswith("kph") or s.endswith("kmh"):
        return float(s[:-3]) * (1000.0/3600.0)
    if s.endswith("km/h"):
        return float(s[:-4]) * (1000.0/3600.0)
    try:
        val = float(s)
        warn("Jednostki prędkości nie rozpoznano – przyjmuję m/s.")
        return val
    except:
        raise ValueError("Niepoprawna prędkość. Użyj np. '5ms' lub '5kph'.")

# ------------------------------ Geodezja na sferze ------------------------------

EARTH_RADIUS_M = 6371000.0

def haversine_m(lat1, lon1, lat2, lon2):
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2.0)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlmb/2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_M * c

def slerp_latlon(lat1, lon1, lat2, lon2, f):
    phi1 = math.radians(lat1); lmb1 = math.radians(lon1)
    phi2 = math.radians(lat2); lmb2 = math.radians(lon2)
    x1 = math.cos(phi1) * math.cos(lmb1)
    y1 = math.cos(phi1) * math.sin(lmb1)
    z1 = math.sin(phi1)
    x2 = math.cos(phi2) * math.cos(lmb2)
    y2 = math.cos(phi2) * math.sin(lmb2)
    z2 = math.sin(phi2)
    dot = x1*x2 + y1*y2 + z1*z2
    dot = max(min(dot, 1.0), -1.0)
    omega = math.acos(dot)
    if omega == 0.0:
        return (lat1, lon1)
    sin_omega = math.sin(omega)
    t1 = math.sin((1.0 - f) * omega) / sin_omega
    t2 = math.sin(f * omega) / sin_omega
    x = t1 * x1 + t2 * x2
    y = t1 * y1 + t2 * y2
    z = t1 * z1 + t2 * z2
    phi = math.atan2(z, math.sqrt(x*x + y*y))
    lmb = math.atan2(y, x)
    return (math.degrees(phi), (math.degrees(lmb) + 540.0) % 360.0 - 180.0)

# ------------------------------ Generowanie punktów ------------------------------

def generate_points_along_polyline(ctrl_pts, step_m):
    if step_m <= 0:
        raise ValueError("Interwał odległości musi być dodatni.")
    pts = []
    total_segments = 0
    total_inserted = 0
    for i in range(len(ctrl_pts)-1):
        A = ctrl_pts[i]
        B = ctrl_pts[i+1]
        seg_len = haversine_m(A[0], A[1], B[0], B[1])
        if i == 0:
            pts.append(A)
        if seg_len == 0.0:
            warn(f"Odcinek {i+1}: długość 0 m – pomijam interpolację.")
            continue
        total_segments += 1
        n_full = int(seg_len // step_m)
        for k in range(1, n_full+1):
            f = (k * step_m) / seg_len
            if f < 1.0:
                P = slerp_latlon(A[0], A[1], B[0], B[1], f)
                pts.append(P)
                total_inserted += 1
        pts.append(B)
    return pts, total_segments, total_inserted

# ------------------------------ Format czasu ------------------------------

def fmt_iso_ms(dt):
    ms = int(round(dt.microsecond / 1000.0))
    if ms == 1000:
        dt = dt + timedelta(milliseconds=1)
        ms = 0
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"

# ------------------------------ Dystrybucja czasu ------------------------------

def distribute_times_distance_linear(points, t_start, t_end):
    if len(points) < 2:
        raise ValueError("Wymagane co najmniej 2 punkty do wyznaczenia czasu.")
    dists = [0.0]
    cum = 0.0
    for i in range(1, len(points)):
        d = haversine_m(points[i-1][0], points[i-1][1], points[i][0], points[i][1])
        cum += d
        dists.append(cum)
    total = dists[-1]
    times = []
    if total == 0.0:
        warn("Dystans całkowity 0 m – czasy rozłożone równomiernie.")
        for i in range(len(points)):
            f = i / (len(points)-1) if len(points) > 1 else 0.0
            dt = t_start + (t_end - t_start) * f
            times.append(dt)
        return times
    T = (t_end - t_start).total_seconds()
    for s in dists:
        f = s / total
        dt = t_start + timedelta(seconds=T * f)
        times.append(dt)
    return times

def distribute_times_by_speed(points, t_start, speed_mps):
    times = [t_start]
    t = t_start
    for i in range(1, len(points)):
        d = haversine_m(points[i-1][0], points[i-1][1], points[i][0], points[i][1])
        dt_sec = d / speed_mps if speed_mps > 0 else 0.0
        t = t + timedelta(seconds=dt_sec)
        times.append(t)
    return times

def enforce_constant_speed(points, t_start, t_end):
    return distribute_times_distance_linear(points, t_start, t_end)

# ------------------------------ Zapis GPX ------------------------------

def save_gpx(points, times, out_name_base="synthetic_path"):
    if len(points) != len(times):
        raise ValueError("Liczba punktów i czasów musi być równa.")
    # Skumulowany dystans i indeksy odcinków (przyrostowo)
    cum = 0.0
    seg_idx = 0
    cum_list = [0.0]
    seg_idx_list = [0]
    for i in range(1, len(points)):
        d = haversine_m(points[i-1][0], points[i-1][1], points[i][0], points[i][1])
        if d > 0:
            seg_idx += 0  # placeholder na przyszłe rozróżnianie odcinków po punktach kontrolnych
        cum += d
        cum_list.append(cum)
        seg_idx_list.append(seg_idx)
    ts_suffix = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_name = f"{out_name_base}_{ts_suffix}.gpx"
    path = os.path.join(os.getcwd(), out_name)
    info(f"Generuję plik wyjściowy: {out_name}")
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<gpx version="1.1" creator="GSV Synthetic GPX" xmlns="http://www.topografix.com/GPX/1/1">\n')
        f.write('  <trk>\n')
        f.write('    <name>Synthetic Path</name>\n')
        f.write('    <trkseg>\n')
        for idx, ((lat, lon), t) in enumerate(zip(points, times), start=1):
            f.write(f'      <trkpt lat="{lat:.8f}" lon="{lon:.8f}">')
            f.write(f'<ele>0000.0</ele><time>{fmt_iso_ms(t)}</time>')
            f.write('<extensions>')
            f.write(f'<gsv:idx xmlns:gsv="https://gsv.local/schema">{idx}</gsv:idx>')
            f.write(f'<gsv:cum_dist_m xmlns:gsv="https://gsv.local/schema">{cum_list[idx-1]:.3f}</gsv:cum_dist_m>')
            f.write(f'<gsv:seg_idx xmlns:gsv="https://gsv.local/schema">{seg_idx_list[idx-1]}</gsv:seg_idx>')
            f.write('</extensions>')
            f.write('</trkpt>\n')
        f.write('    </trkseg>\n')
        f.write('  </trk>\n')
        f.write('</gpx>\n')
    ok(f"Zapisano: {path}")
    return path

# ------------------------------ Główna logika ------------------------------

def main():
    print("[#] START: GPX Synthetic Path Generator (v2.3)")
    print("--------------------------------------------------------------")
    print("Wklej punkty: 'lat, lon' (kropka dziesiętna). Separatory: ',', ';', spacja/tab, '|'.")
    print("Skrypt SAM znormalizuje do 8 miejsc (skróci/podstawi zera w zapisie).")
    print("Pusta linia kończy podawanie punktów.\n")

    ctrl_pts = []
    idx = 1
    while True:
        try:
            line = input(f"[?] Podaj punkt #{idx} (lat, lon) lub Enter, aby zakończyć: ").strip()
        except KeyboardInterrupt:
            err("Przerwano przez użytkownika.")
            wait_or_exit()
            return
        if line == "":
            if len(ctrl_pts) < 2:
                err("Wymagane co najmniej 2 punkty. Spróbuj ponownie.")
                continue
            else:
                break
        try:
            lat, lon = parse_latlon(line)
            ctrl_pts.append((lat, lon))
            ok(f"Zapisano punkt #{idx}: lat={lat:.8f}, lon={lon:.8f}")
            idx += 1
        except Exception as e:
            err(f"Błąd punktu: {e}")

    info(f"Liczba punktów kontrolnych: {len(ctrl_pts)}")

    # Interwał odległości
    print("\n[=] USTAWIENIA ODLEGŁOŚCI")
    print("Podaj interwał między punktami w CENTYMETRACH (np. 100 = 1 m).")
    while True:
        s = input("[?] Interwał odległości [cm]: ").strip()
        try:
            cm = float(s)
            if cm <= 0:
                raise ValueError
            step_m = cm / 100.0
            ok(f"Interwał {cm} cm = {step_m:.3f} m")
            break
        except:
            err("Nieprawidłowa wartość – podaj dodatnią liczbę.")

    # Generowanie geometrii
    info("Interpoluję punkty wzdłuż odcinków...")
    points, seg_count, inserted = generate_points_along_polyline(ctrl_pts, step_m)
    ok(f"Interpolacja zakończona. Odcinków: {seg_count}, wstawionych punktów: {inserted}.")
    ok(f"Łączna liczba punktów wynikowych: {len(points)}")

    # Czas startowy
    print("\n[=] USTAWIENIA CZASU")
    while True:
        try:
            tstart_str = input("[?] Podaj CZAS POCZĄTKOWY 1. punktu (np. 2025-05-01T23:04:44.500): ").strip()
            t_start = parse_start_or_end_time(tstart_str)
            ok(f"Czas początkowy: {t_start.isoformat(timespec='milliseconds')}")
            break
        except Exception as e:
            err(f"Błąd czasu: {e}")

    # Wybór trybu
    print("\n[=] Wybierz tryb wyliczania czasów dla kolejnych punktów:")
    print("    (t) Ustawić PRĘDKOŚĆ (ms/kmh) -> czasy z prędkości")
    print("    (k) Ustawić CZAS KOŃCOWY     -> rozkład proporcjonalny po dystansie")
    while True:
        mode = input("[?] Wpisz 't' lub 'k': ").strip().lower()
        if mode in ("t", "k"):
            break
        else:
            err("Wpisz dokładnie 't' lub 'k'.")

    # Wyliczenie czasów
    if mode == "t":
        while True:
            try:
                v_str = input("[?] Podaj prędkość (np. 5ms, 4.5ms, 5kph/5kmh): ").strip()
                v = parse_speed(v_str)
                if v <= 0:
                    raise ValueError("Prędkość musi być dodatnia.")
                ok(f"Prędkość: {v:.3f} m/s")
                break
            except Exception as e:
                err(f"Błąd prędkości: {e}")
        info("Wyznaczam czasy na podstawie prędkości...")
        times = distribute_times_by_speed(points, t_start, v)
        ok("Wyznaczono czasy (tryb prędkości).")
        t_end = times[-1]
    else:
        while True:
            try:
                tend_str = input("[?] Podaj CZAS KOŃCOWY ostatniego punktu: ").strip()
                t_end = parse_start_or_end_time(tend_str)
                if t_end <= t_start:
                    raise ValueError("Czas końcowy musi być po czasie początkowym.")
                ok(f"Czas końcowy: {t_end.isoformat(timespec='milliseconds')}")
                break
            except Exception as e:
                err(f"Błąd czasu końcowego: {e}")
        info("Interpoluję czasy proporcjonalnie do dystansu...")
        times = distribute_times_distance_linear(points, t_start, t_end)
        ok("Wyznaczono czasy (tryb czas końcowy).")

    # Opcjonalne wyrównanie do stałej prędkości (tak/nie)
    print("\n[=] Czy chcesz WYRÓWNAĆ czasy do STAŁEJ PRĘDKOŚCI na podstawie całkowitego czasu?")
    print("    Uwaga: oba tryby t/k i tak dają stałą średnią prędkość; ta opcja dodatkowo")
    print("    przelicza czasy z dokładnym rozkładem liniowym po dystansie między t_start a t_end.")
    yn = input("[?] Wpisz 't' dla tak lub cokolwiek innego dla nie: ").strip().lower()
    if yn == 't':
        info("Wyrównuję czasy do stałej prędkości (interpolacja liniowa po dystansie z t_start..t_end)...")
        times = enforce_constant_speed(points, t_start, t_end)
        ok("Czasy wyrównane.")

    # Podsumowanie dystansu i czasu
    total_dist = 0.0
    for i in range(1, len(points)):
        total_dist += haversine_m(points[i-1][0], points[i-1][1], points[i][0], points[i][1])
    ok(f"Łączny dystans: {total_dist:.2f} m")
    ok(f"Łączny czas: {(times[-1] - times[0])} (HH:MM:SS.micro)")

    # Zapis GPX
    path = save_gpx(points, times, out_name_base="synthetic_path")

    print("\n--- PODSUMOWANIE MISJI ---")
    print(f"Punkty kontrolne (wejście): {len(ctrl_pts)}")
    print(f"Odcinków: {seg_count}")
    print(f"Wstawionych punktów: {inserted}")
    print(f"Punkty wynikowe (wyjście): {len(points)}")
    print(f"Interwał (m): {step_m:.3f}")
    print(f"Długość ścieżki (m): {total_dist:.2f}")
    print(f"Czas startowy: {fmt_iso_ms(times[0])}")
    print(f"Czas końcowy:  {fmt_iso_ms(times[-1])}")
    print(f"Plik wyjściowy: {path}")
    print("---------------------------")
    wait_or_exit()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err(f"Nieoczekiwany błąd krytyczny: {e}")
        wait_or_exit()
