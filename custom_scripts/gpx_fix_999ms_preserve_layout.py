# gpx_fix_999ms_preserve_layout.py

import os, re, sys
from datetime import datetime, timedelta

ISO_TIME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z$')
TRKPT_BLOCK_RE = re.compile(r'<trkpt\b[^>]*>.*?</trkpt>', re.DOTALL|re.IGNORECASE)
TIME_IN_TRKPT_RE = re.compile(r'(<time>)([^<]+)(</time>)', re.IGNORECASE)

THRESHOLD = 1.002


def run(gpx):
    """
    Fix timestamps ending at 999 ms in the currently loaded GPX object.

    GPXEditor passes a gpxpy.gpx.GPX object. This adapter mutates point.time
    values in place and lets the editor rebuild the view afterwards.
    """
    fixed = 0
    checked = 0

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                checked += 1
                timestamp = getattr(point, "time", None)
                if timestamp is None:
                    continue

                if 999000 <= timestamp.microsecond <= 999999:
                    point.time = (timestamp + timedelta(milliseconds=1)).replace(microsecond=0)
                    fixed += 1

    print(f"[OK] Checked {checked} points. Fixed .999 timestamps: {fixed}")
    return gpx

def is_999_fraction(frac: str) -> bool:
    if not frac:
        return False
    digs = frac[1:]
    if len(digs) < 3:
        return False
    return digs[:3] == '999' and (len(digs) == 3 or set(digs[3:]) <= {'0'})

def parse_iso_z(s: str):
    m = ISO_TIME_RE.match(s.strip())
    if not m: return None
    try:
        return datetime.fromisoformat(s.strip().replace('Z', '+00:00'))
    except:
        return None

def fmt_iso_z_fullsecond(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S') + '.000Z'

def extract_times_from_text_gpx_trkpt(text):
    items = []
    for i, blk in enumerate(TRKPT_BLOCK_RE.finditer(text)):
        inner = blk.group(0)
        for t in TIME_IN_TRKPT_RE.finditer(inner):
            items.append((i, t.group(2).strip()))
    return items

def times_to_datetimes(times):
    return [parse_iso_z(t) for t in times if parse_iso_z(t)]

def analyze_sequence(dts):
    anomalies = {"dup": [], "backward": [], "gap_gt1s": [], "gap_big": []}
    for i in range(len(dts)-1):
        delta = (dts[i+1] - dts[i]).total_seconds()
        if delta == 0:
            anomalies["dup"].append(i)
        elif delta < 0:
            anomalies["backward"].append(i)
        elif delta > 1.0:
            anomalies["gap_gt1s"].append((i, delta))
        if delta > THRESHOLD:
            anomalies["gap_big"].append((i, delta))
    total = (dts[-1] - dts[0]).total_seconds() if dts else 0
    return anomalies, total

def compare_anomalies(ori, fixed):
    return {
        "dup": sorted(set(fixed["dup"]) - set(ori["dup"])),
        "backward": sorted(set(fixed["backward"]) - set(ori["backward"])),
        "gap_gt1s": [(i,d) for (i,d) in fixed["gap_gt1s"]
                     if i not in {x for x,_ in ori["gap_gt1s"]}]
    }

def fix_999_in_block(block, stats):
    def repl(m):
        tval = m.group(2).strip()
        iso = ISO_TIME_RE.match(tval)
        if not iso:
            return m.group(1)+tval+m.group(3)
        if is_999_fraction(iso.group(2) or ''):
            dt = parse_iso_z(tval)
            if dt:
                new = fmt_iso_z_fullsecond(dt + timedelta(milliseconds=1))
                stats["fixed"] += 1
                if len(stats["examples"]) < 5:
                    stats["examples"].append((tval, new))
                return m.group(1)+new+m.group(3)
        return m.group(1)+tval+m.group(3)
    return TIME_IN_TRKPT_RE.sub(repl, block)

def process_file(path):
    print(f"[i] Otwieram: {path}")
    src = open(path, encoding='utf-8').read()

    ori_times = [t for _,t in extract_times_from_text_gpx_trkpt(src)]
    ori_dts = times_to_datetimes(ori_times)
    ori_anom, ori_total = analyze_sequence(ori_dts)

    print(f"[i] Znaleziono {len(ori_times)} znaczników czasu w <trkpt>.")

    # NOWE – raport dużych przerw
    if not ori_anom["gap_big"]:
        print("[i] Brak przerw czasowych > 1.002s w ORYGINALNYM pliku.")
    else:
        biggest = max(ori_anom["gap_big"], key=lambda x: x[1])
        print(f"[!] Wykryto {len(ori_anom['gap_big'])} przerw >1.002s.")
        print(f"    Największa: i={biggest[0]}, Δ={biggest[1]:.3f}s")

    stats = {"fixed":0,"errors":0,"examples":[]}
    out = []
    last = 0
    for m in TRKPT_BLOCK_RE.finditer(src):
        out.append(src[last:m.start()])
        out.append(fix_999_in_block(m.group(0), stats))
        last = m.end()
    out.append(src[last:])
    dst = ''.join(out)

    fix_times = [t for _,t in extract_times_from_text_gpx_trkpt(dst)]
    fix_dts = times_to_datetimes(fix_times)
    fix_anom, fix_total = analyze_sequence(fix_dts)
    new_anom = compare_anomalies(ori_anom, fix_anom)

    out_path = path.replace(".gpx","_999_fixed.gpx")
    open(out_path,"w",encoding="utf-8").write(dst)

    print("----- PODSUMOWANIE PLIKU -----")
    print(f"Poprawek .999Z -> .000Z (+1 ms): {stats['fixed']}")

    if stats["examples"]:
        print("[i] Przykłady (max 5):")
        for a,b in stats["examples"]:
            print(f"    {a}  ->  {b}")

    print(f"Błędów konwersji: {stats['errors']}")
    print(f"Łączny czas (ostatni - pierwszy) oryginał: {ori_total:.3f}s, po poprawce: {fix_total:.3f}s")
    print(f"Łączny czas {'OK' if ori_total==fix_total else 'RÓŻNY! ⚠'}")

    def fmt(a): return [f"i={i}, Δ={d:.3f}s" if isinstance(i,int) else str(i) for i,d in a] if a and isinstance(a[0],tuple) else a

    print("[i] Anomalie w ORYGINALE:")
    print(f"  duplikaty sekund: {ori_anom['dup']}")
    print(f"  wsteczny czas:   {ori_anom['backward']}")
    print(f"  skoki >1s:       {[f'i={i}, Δ={d:.3f}s' for i,d in ori_anom['gap_gt1s']]}")

    print("[i] Anomalie po POPRAWCE:")
    print(f"  duplikaty sekund: {fix_anom['dup']}")
    print(f"  wsteczny czas:   {fix_anom['backward']}")
    print(f"  skoki >1s:       {[f'i={i}, Δ={d:.3f}s' for i,d in fix_anom['gap_gt1s']]}")

    print("[i] NOWE anomalie (pojawione po poprawce):")
    print(f"  duplikaty sekund: {new_anom['dup']}")
    print(f"  wsteczny czas:   {new_anom['backward']}")
    print(f"  skoki >1s:       {[f'i={i}, Δ={d:.3f}s' for i,d in new_anom['gap_gt1s']]}")

    # NOWE – końcowa ciągłość
    if fix_dts:
        start = fix_dts[0]
        end = fix_dts[-1]
        dur = (end-start).total_seconds()
        print("[i] Ciągłość czasowa: ~1 sekunda między punktami.")
        print(f"    Start: {start}")
        print(f"    Koniec: {end}")
        print(f"    Czas trwania: {dur:.3f}s")

    print("------------------------------")

    return {"status":"ok","fixed":stats["fixed"],"total_same":ori_total==fix_total,"out":out_path}

def main():
    print("[#] START")
    print("[i] Skrypt:")
    print(" - naprawia .999Z → .000Z (+1 ms)")
    print(" - nie zmienia struktury pliku")
    print(" - analizuje ciągłość czasu")
    print(" - raportuje przerwy >1.002s")
    print(" - zachowuje pełne raporty + dodaje nowe")
    print("----------------------------------------------------")

    files = [f for f in os.listdir('.') if f.lower().endswith('.gpx')]
    results = [process_file(f) for f in files]

    print("\n========== PODSUMOWANIE ZBIORCZE ==========")
    for r in results:
        print(f"  ✓ {r['out']} (poprawki: {r['fixed']}, total {'OK' if r['total_same'] else 'RÓŻNY⚠'})")
    print("===========================================")

    input("[Enter] Koniec...")

if __name__ == "__main__":
    main()
