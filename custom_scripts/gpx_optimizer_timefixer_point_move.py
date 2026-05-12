# ============================================================
# GPX STRAIGHTENER / TIME FIXER / TRACK OPTIMIZER
# Wersja: 1.0
# Python 3.x
# ============================================================

import os
import re
import math
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

try:
    import gpxpy.gpx
except Exception:
    gpxpy = None

# ============================================================
# KONFIG
# ============================================================

TIME_TOLERANCE_MS = 2


def run(gpx, context):
    """
    GPXEditor adapter.

    Straightens the currently selected point range in the active GPX segment.
    Select at least two points in GPXEditor first. The first and last selected
    points stay as anchors; points between them are moved onto a straight line.
    Timestamps and other point metadata are preserved.
    """
    selected = sorted(context.selected_indices)
    if len(selected) < 2:
        raise RuntimeError("Select at least two points before running this script.")

    start_i = selected[0]
    end_i = selected[-1]
    editor = context.editor
    segment = editor.segment
    if segment is None or not segment.points:
        raise RuntimeError("No active GPX segment is loaded.")

    if start_i < 0 or end_i >= len(segment.points) or end_i <= start_i:
        raise RuntimeError("Selected point range is not valid for the current segment.")

    points = [
        {
            "lat": point.latitude,
            "lon": point.longitude,
            "time": point.time,
            "xml": None,
        }
        for point in segment.points
    ]

    before_length = track_length(points[start_i:end_i + 1])
    optimized = optimize_segment(points, start_i, end_i, noise_percent=0)
    after_length = track_length(optimized)

    for point, optimized_point in zip(segment.points[start_i:end_i + 1], optimized):
        point.latitude = optimized_point["lat"]
        point.longitude = optimized_point["lon"]

    print(
        "[OK] Straightened selected range "
        f"{start_i}-{end_i}. Length {before_length:.2f} m -> {after_length:.2f} m."
    )
    return gpx

# ============================================================
# POMOCNICZE
# ============================================================

def wait_exit():
    input("\n[ENTER] Zakończ...")

def meters_distance(lat1, lon1, lat2, lon2):
    R = 6371000

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(dlambda / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c

def bearing_deg(lat1, lon1, lat2, lon2):
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (
        math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
        - math.sin(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.cos(math.radians(lon2 - lon1))
    )

    brng = math.degrees(math.atan2(y, x))
    return (brng + 360) % 360

def move_point(lat, lon, meters, angle_deg):
    R = 6371000

    brng = math.radians(angle_deg)

    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(meters / R)
        + math.cos(lat1) * math.sin(meters / R) * math.cos(brng)
    )

    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(meters / R) * math.cos(lat1),
        math.cos(meters / R) - math.sin(lat1) * math.sin(lat2),
    )

    return math.degrees(lat2), math.degrees(lon2)

def format_hms(seconds):
    ms = int((seconds - int(seconds)) * 1000)

    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)

    return f"{h:02}:{m:02}:{s:02}.{ms:03}"

def parse_time(t):
    try:
        return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%fZ")
    except:
        try:
            return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ")
        except:
            return None

def time_to_string(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

# ============================================================
# GPX
# ============================================================

def load_gpx(file_path):

    tree = ET.parse(file_path)
    root = tree.getroot()

    pts = []

    for trkpt in root.iter():
        if trkpt.tag.endswith("trkpt"):

            lat = float(trkpt.attrib["lat"])
            lon = float(trkpt.attrib["lon"])

            time_node = None

            for c in trkpt:
                if c.tag.endswith("time"):
                    time_node = c
                    break

            dt = None

            if time_node is not None:
                dt = parse_time(time_node.text)

            pts.append({
                "lat": lat,
                "lon": lon,
                "time": dt,
                "xml": trkpt
            })

    return tree, root, pts

# ============================================================
# ANALIZA CZASU
# ============================================================

def analyze_time(points):

    print("\n===================================================")
    print("ANALIZA CZASU")
    print("===================================================")

    with_time = [p for p in points if p["time"] is not None]

    if len(with_time) == 0:
        print("[WARNING] Brak timestamp w całym pliku.")
        return "no_time"

    missing = len(points) - len(with_time)

    if missing > 0:
        print(f"[WARNING] Brakuje timestampów: {missing}")

    intervals = []

    for i in range(1, len(with_time)):
        delta = (
            with_time[i]["time"] - with_time[i - 1]["time"]
        ).total_seconds()

        intervals.append(delta)

    if len(intervals) == 0:
        return "bad"

    min_i = min(intervals)
    max_i = max(intervals)

    print(f"[INFO] Minimalny interwał: {min_i:.3f}s")
    print(f"[INFO] Maksymalny interwał: {max_i:.3f}s")

    stable = True

    for x in intervals:
        if abs(x - min_i) > 0.002:
            stable = False
            break

    if stable:
        print("[OK] Interwał stały.")
        return "stable"

    if max_i <= min_i * 1.9:
        print("[INFO] Interwał niestały ale dopuszczalny.")
        return "acceptable"

    print("[WARNING] Wykryto duże luki czasowe.")

    return "gaps"

# ============================================================
# INTERPOLACJA LUK
# ============================================================

def interpolate_gaps(points):

    print("\n[INFO] Interpolacja luk czasowych...")

    new_points = []

    for i in range(len(points) - 1):

        p1 = points[i]
        p2 = points[i + 1]

        new_points.append(p1)

        if p1["time"] and p2["time"]:

            delta = (
                p2["time"] - p1["time"]
            ).total_seconds()

            if delta > 1.5:

                missing = int(delta) - 1

                for k in range(missing):

                    t = (k + 1) / (missing + 1)

                    lat = p1["lat"] + (p2["lat"] - p1["lat"]) * t
                    lon = p1["lon"] + (p2["lon"] - p1["lon"]) * t

                    dt = p1["time"] + timedelta(seconds=(k + 1))

                    new_points.append({
                        "lat": lat,
                        "lon": lon,
                        "time": dt,
                        "xml": None
                    })

    new_points.append(points[-1])

    print(f"[OK] Dodano punktów: {len(new_points)-len(points)}")

    return new_points

# ============================================================
# ZAPIS GPX
# ============================================================

def save_gpx_simple(points, output_path):

    with open(output_path, "w", encoding="utf-8") as f:

        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<gpx version="1.0">\n')
        f.write('<trk>\n')
        f.write('<trkseg>\n')

        for p in points:

            line = (
                f'<trkpt lat="{p["lat"]:.7f}" '
                f'lon="{p["lon"]:.7f}">'
            )

            if p["time"] is not None:
                line += f'<time>{time_to_string(p["time"])}</time>'

            line += '</trkpt>\n'

            f.write(line)

        f.write('</trkseg>\n')
        f.write('</trk>\n')
        f.write('</gpx>\n')

# ============================================================
# WYBÓR PUNKTU
# ============================================================

def select_point(points, text):

    while True:

        print("\n===================================================")
        print(text)
        print("===================================================")

        val = input("> ").strip().lower()

        if val == "q":
            return None, None

        # ====================================================
        # p245
        # ====================================================

        if val.startswith("p"):

            try:
                idx = int(val[1:])

                if idx < 0 or idx >= len(points):
                    print(f"[ERROR] Maksymalny numer: {len(points)-1}")
                    continue

                return idx, None

            except:
                print("[ERROR] Niepoprawny numer.")
                continue

        # ====================================================
        # CZAS
        # ====================================================

        if ":" in val or val.isdigit():

            try:

                cleaned = re.sub(r"[^0-9]", "", val)

                if len(cleaned) == 6:

                    hh = int(cleaned[0:2])
                    mm = int(cleaned[2:4])
                    ss = int(cleaned[4:6])

                    for i, p in enumerate(points):

                        if p["time"] is None:
                            continue

                        t = p["time"]

                        if (
                            t.hour == hh
                            and t.minute == mm
                            and t.second == ss
                        ):
                            return i, None

                    print("[ERROR] Nie znaleziono czasu.")
                    continue

            except:
                pass

        # ====================================================
        # WSPÓŁRZĘDNE
        # ====================================================

        coord = re.findall(r"[-+]?\d+\.\d+", val)

        if len(coord) >= 2:

            lat = round(float(coord[0]), 7)
            lon = round(float(coord[1]), 7)

            best_i = None
            best_d = 999999999

            for i, p in enumerate(points):

                d = meters_distance(
                    lat,
                    lon,
                    p["lat"],
                    p["lon"]
                )

                if d < best_d:
                    best_d = d
                    best_i = i

            print("\n[INFO] Najbliższy punkt:")
            print(f"Index: {best_i}")
            print(f"Odległość: {best_d:.2f} m")

            print("\nOPCJE:")
            print("Y = użyj najbliższego punktu z pliku")
            print("N = nie używaj")
            print("C = użyj MOICH współrzędnych")
            print("    ale przypisz je do tego punktu")
            print("    (zmieni współrzędne punktu źródłowego)")

            ans = input("\nWybór Y/N/C: ").lower()

            if ans == "y":
                return best_i, None

            elif ans == "c":

                custom_override = {
                    "lat": lat,
                    "lon": lon
                }

                return best_i, custom_override

            else:
                continue

# ============================================================
# OPTYMALIZACJA
# ============================================================

def optimize_segment(points, start_i, end_i, noise_percent):

    if end_i <= start_i:
        raise Exception("END <= START")

    segment = points[start_i:end_i + 1]

    start = segment[0]
    end = segment[-1]

    total_pts = len(segment)

    straight = []

    for i in range(total_pts):

        t = i / (total_pts - 1)

        lat = start["lat"] + (end["lat"] - start["lat"]) * t
        lon = start["lon"] + (end["lon"] - start["lon"]) * t

        straight.append({
            "lat": lat,
            "lon": lon,
            "time": segment[i]["time"],
            "xml": None
        })

    if noise_percent > 0:

        print("\n[INFO] Dodawanie pseudolosowego szumu...")

        noisy = [straight[0]]

        for i in range(1, len(straight)):

            base_prev = straight[i - 1]
            base_cur = straight[i]

            d = meters_distance(
                base_prev["lat"],
                base_prev["lon"],
                base_cur["lat"],
                base_cur["lon"]
            )

            noise = d * (noise_percent / 100.0)

            angle = random.uniform(1, 359)

            lat2, lon2 = move_point(
                base_cur["lat"],
                base_cur["lon"],
                random.uniform(0, noise),
                angle
            )

            noisy.append({
                "lat": lat2,
                "lon": lon2,
                "time": base_cur["time"],
                "xml": None
            })

        straight = noisy

    return straight

# ============================================================
# DŁUGOŚĆ ŚLADU
# ============================================================

def track_length(points):

    total = 0

    for i in range(1, len(points)):

        total += meters_distance(
            points[i - 1]["lat"],
            points[i - 1]["lon"],
            points[i]["lat"],
            points[i]["lon"]
        )

    return total

# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("GPX STRAIGHTENER / TIME FIXER / TRACK OPTIMIZER")
    print("=" * 60)

    print("\nFUNKCJE:")
    print("- analiza czasu")
    print("- naprawa timestamp")
    print("- interpolacja luk")
    print("- prostowanie śladu")
    print("- usuwanie zygzaków")
    print("- pseudolosowy szum GPS")
    print("- zapis GPX 1 punkt = 1 linia")

    files = [
        x for x in os.listdir(".")
        if x.lower().endswith(".gpx")
    ]

    if not files:
        print("\n[ERROR] Brak plików GPX.")
        wait_exit()
        return

    print("\nZNALEZIONE PLIKI:")

    for i, f in enumerate(files):
        print(f"{i+1}. {f}")

    sel = input("\nWybierz numer pliku: ")

    try:
        file_path = files[int(sel)-1]
    except:
        print("[ERROR] Niepoprawny wybór.")
        wait_exit()
        return

    print(f"\n[INFO] Wczytywanie: {file_path}")

    tree, root, points = load_gpx(file_path)

    print(f"[OK] Wczytano punktów: {len(points)}")

    status = analyze_time(points)

    if status == "no_time":

        ans = input("\nCzy wygenerować timestampy? Y/N: ").lower()

        if ans == "y":

            start_txt = input(
                "Podaj czas początkowy "
                "(2026-05-12T12:56:43.500): "
            )

            interval = float(
                input("Podaj interwał sekundowy: ")
            )

            dt0 = parse_time(start_txt + "Z")

            for i, p in enumerate(points):

                p["time"] = dt0 + timedelta(
                    seconds=i * interval
                )

            out = file_path.replace(".gpx", "_timefixed.gpx")

            save_gpx_simple(points, out)

            print(f"[OK] Zapisano: {out}")

            file_path = out

    elif status == "gaps":

        ans = input(
            "\nCzy interpolować luki czasowe? Y/N: "
        ).lower()

        if ans == "y":

            points = interpolate_gaps(points)

            out = file_path.replace(".gpx", "_timefixed.gpx")

            save_gpx_simple(points, out)

            print(f"[OK] Zapisano: {out}")

            file_path = out

    start_i, start_override = select_point(
        points,
        "PODAJ PUNKT STARTOWY"
    )

    if start_i is None:
        return

    end_i, end_override = select_point(
        points,
        "PODAJ PUNKT KOŃCOWY"
    )

    if end_i is None:
        return

    print("\n===================================================")
    print("ANALIZA ODCINKA")
    print("===================================================")

    segment = points[start_i:end_i+1]

    # ============================================================
    # NADPISANIE WSPÓŁRZĘDNYCH START
    # ============================================================

    if start_override is not None:

        segment[0]["lat"] = start_override["lat"]
        segment[0]["lon"] = start_override["lon"]

    # ============================================================
    # NADPISANIE WSPÓŁRZĘDNYCH STOP
    # ============================================================

    if end_override is not None:

        segment[-1]["lat"] = end_override["lat"]
        segment[-1]["lon"] = end_override["lon"]

    t0 = segment[0]["time"]
    t1 = segment[-1]["time"]

    delta = (t1 - t0).total_seconds()

    straight_dist = meters_distance(
        segment[0]["lat"],
        segment[0]["lon"],
        segment[-1]["lat"],
        segment[-1]["lon"]
    )

    track_dist = track_length(segment)

    print(f"Punkt start: {start_i}")
    print(f"Punkt stop : {end_i}")
    print(f"Ilość punktów: {len(segment)}")
    print(f"Czas: {delta:.3f}s")
    print(f"Czas HMS: {format_hms(delta)}")
    print(f"Długość po śladzie: {track_dist:.2f} m")
    print(f"Długość po prostej: {straight_dist:.2f} m")

    speed = track_dist / delta if delta > 0 else 0

    print(f"Średnia prędkość: {speed:.3f} m/s")

    noise = float(
        input(
            "\nPodaj poziom szumu GPS 0-100%: "
        )
    )

    optimized = optimize_segment(
        points,
        start_i,
        end_i,
        noise
    )

    final_points = (
        points[:start_i]
        + optimized
        + points[end_i+1:]
    )

    output = file_path.replace(
        ".gpx",
        "_optimized_ver1.gpx"
    )

    save_gpx_simple(final_points, output)

    print("\n===================================================")
    print("PODSUMOWANIE")
    print("===================================================")

    new_track = track_length(optimized)

    print(f"Plik wynikowy: {output}")
    print(f"Punktów: {len(optimized)}")
    print(f"Długość po śladzie: {new_track:.2f} m")
    print(f"Długość po prostej: {straight_dist:.2f} m")

    if noise > 0:
        print(f"Szum GPS: {noise:.2f}%")
    else:
        print("Brak pseudolosowego szumu.")

    print("\n[OK] Zakończono.")

    again = input(
        "\nCzy optymalizować kolejny odcinek? Y/N: "
    ).lower()

    if again == "y":
        main()
        return

    wait_exit()

# ============================================================

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n[CRITICAL ERROR]")
        print(str(e))
        wait_exit()
