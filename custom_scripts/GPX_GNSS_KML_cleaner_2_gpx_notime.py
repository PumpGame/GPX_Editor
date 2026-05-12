import os
import datetime
from collections import defaultdict
import xml.etree.ElementTree as ET

# =========================
# KONFIG
# =========================
TIME_GAP_THRESHOLD = 1.005

# =========================
# FUNKCJE
# =========================

def format_time(ms):
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, datetime.UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_time_to_ms(timestr):
    try:
        dt = datetime.datetime.fromisoformat(timestr.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except:
        return None


def get_global_output_filename(base_name):
    base = base_name.replace(".txt", "_gps.gpx")\
                    .replace(".gpx", "_cleaned.gpx")\
                    .replace(".kml", "_cleaned.gpx")

    if not os.path.exists(base):
        return base

    version = 2
    while True:
        candidate = base.replace(".gpx", f"_ver{version}.gpx")
        if not os.path.exists(candidate):
            return candidate
        version += 1


def find_input_files():
    files = [f for f in os.listdir() if f.endswith((".txt", ".gpx", ".kml"))]

    if not files:
        print("[!] Nie znaleziono plików .txt / .gpx / .kml")
        exit()

    if len(files) == 1:
        print(f"[i] Znaleziono plik: {files[0]}")
        return [files[0]]

    print("[i] Wybierz plik:")
    for i, f in enumerate(files):
        print(f" {i}: {f}")
    print(" w: wszystkie pliki")

    choice = input("Podaj numer lub 'w': ").lower()

    if choice == "w":
        return files
    else:
        return [files[int(choice)]]

# =========================
# START
# =========================

print("=================================")
print("[#] START - GPS → GPX")
print("=================================")

input_files = find_input_files()

for input_file in input_files:

    try:
        print("\n---------------------------------")
        print(f"[#] Przetwarzanie: {input_file}")
        print("---------------------------------")

        output_file = get_global_output_filename(input_file)

        print(f"[i] Otwieram: {input_file}")

        points = []  # (time_ms OR None, lat, lon)
        missing_time_count = 0

        # =========================
        # TXT (GNSS)
        # =========================
        if input_file.endswith(".txt"):

            with open(input_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("Fix,GPS"):
                        parts = line.strip().split(",")

                        try:
                            lat = round(float(parts[2]), 7)
                            lon = round(float(parts[3]), 7)
                            time_ms = int(parts[8])

                            points.append((time_ms, lat, lon))
                        except:
                            continue

        # =========================
        # GPX
        # =========================
        elif input_file.endswith(".gpx"):

            tree = ET.parse(input_file)
            root = tree.getroot()

            ns_uri = root.tag.split("}")[0].strip("{")
            ns = {"gpx": ns_uri}

            for trkpt in root.findall(".//gpx:trkpt", ns):
                try:
                    lat = round(float(trkpt.attrib["lat"]), 7)
                    lon = round(float(trkpt.attrib["lon"]), 7)

                    time_elem = trkpt.find("gpx:time", ns)

                    if time_elem is None:
                        missing_time_count += 1
                        points.append((None, lat, lon))
                        continue

                    time_ms = parse_time_to_ms(time_elem.text)

                    if time_ms is None:
                        missing_time_count += 1
                        points.append((None, lat, lon))
                        continue

                    points.append((time_ms, lat, lon))
                except:
                    continue

        # =========================
        # KML
        # =========================
        elif input_file.endswith(".kml"):

            tree = ET.parse(input_file)
            root = tree.getroot()

            ns = {"kml": "http://www.opengis.net/kml/2.2"}

            coords = root.findall(".//kml:coordinates", ns)

            for block in coords:
                lines = block.text.strip().split()

                for line in lines:
                    try:
                        lon, lat, *_ = line.split(",")
                        lat = round(float(lat), 7)
                        lon = round(float(lon), 7)

                        missing_time_count += 1
                        points.append((None, lat, lon))
                    except:
                        continue

        print(f"[i] Wczytano punktów GPS: {len(points)}")

        # =========================
        # SORTOWANIE (tylko jeśli czas istnieje)
        # =========================

        if any(p[0] is not None for p in points):
            print("[i] Sortowanie po czasie...")
            points.sort(key=lambda x: (x[0] is None, x[0]))

        # =========================
        # DUPLIKATY CZASU
        # =========================

        print("[i] Analiza duplikatów czasu...")

        time_counts = defaultdict(int)

        for t, lat, lon in points:
            if t is not None:
                time_counts[t] += 1

        dup_time = [k for k, v in time_counts.items() if v > 1]

        print(f"[i] Duplikaty czasu: {len(dup_time)}")

        if dup_time:
            choice = input("[?] Usunąć duplikaty czasu? (t/n): ").lower()

            if choice == "t":
                seen_time = set()
                new_points = []

                for t, lat, lon in points:
                    if t is not None and t in seen_time:
                        continue

                    if t is not None:
                        seen_time.add(t)

                    new_points.append((t, lat, lon))

                print(f"[i] Usunięto duplikaty. Nowa liczba punktów: {len(new_points)}")
                points = new_points

        # =========================
        # ANALIZA CZASU (tylko jeśli jest czas)
        # =========================

        if any(p[0] is not None for p in points):
            print("[i] Analiza ciągłości czasu...")

            max_gap = 0
            max_gap_index = -1
            gaps = []

            timed_points = [p for p in points if p[0] is not None]

            for i in range(1, len(timed_points)):
                dt = (timed_points[i][0] - timed_points[i-1][0]) / 1000.0

                if dt > TIME_GAP_THRESHOLD:
                    gaps.append((i, dt))

                if dt > max_gap:
                    max_gap = dt
                    max_gap_index = i

            print(f"[i] Liczba przerw > {TIME_GAP_THRESHOLD}s: {len(gaps)}")

            if max_gap_index != -1:
                t1 = format_time(timed_points[max_gap_index-1][0])
                t2 = format_time(timed_points[max_gap_index][0])

                print("[i] Największa przerwa:")
                print(f"    {max_gap:.3f}s")
                print(f"    między: {t1} → {t2}")
        else:
            print("[i] Brak danych czasowych → pomijam analizę czasu")

        # =========================
        # ZAPIS GPX
        # =========================

        print("[i] Generowanie GPX...")

        with open(output_file, "w", encoding="utf-8") as f:
            f.write("<gpx>\n<trk>\n<trkseg>\n\n")

            for t, lat, lon in points:
                if t is None:
                    f.write(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"></trkpt>\n')
                else:
                    time_str = format_time(t)
                    f.write(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"><time>{time_str}</time></trkpt>\n')

            f.write("\n</trkseg>\n</trk>\n</gpx>\n")

        # =========================
        # PODSUMOWANIE
        # =========================

        print("=================================")
        print("[#] PODSUMOWANIE")
        print("=================================")
        print(f"[i] Plik wynikowy: {output_file}")
        print(f"[i] Liczba punktów: {len(points)}")

        if missing_time_count > 0:
            if missing_time_count == len(points):
                print("[!] CAŁKOWITY BRAK DANYCH CZASU")
            else:
                print(f"[!] Punkty bez czasu: {missing_time_count}")

    except Exception as e:
        print(f"[!] Błąd w pliku {input_file}: {e}")
        print("[!] Pomijam plik i lecę dalej...")

print("\n=================================")
print("[#] KONIEC CAŁEGO PROCESU")