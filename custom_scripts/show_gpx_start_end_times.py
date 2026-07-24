def _collect_point_times(points):
    return [point.time for point in points if getattr(point, "time", None) is not None]


def _fmt(timestamp):
    if timestamp is None:
        return "brak"
    return timestamp.isoformat(sep=" ", timespec="seconds")


def run(gpx):
    """
    Print GPX start and end times without modifying any data.
    """
    if not getattr(gpx, "tracks", None):
        print("[!] Brak ścieżek w pliku GPX.")
        return gpx

    total_tracks = 0
    total_segments = 0

    print("[i] Czasy początku i końca ścieżek GPX:")

    for track_index, track in enumerate(gpx.tracks, start=1):
        total_tracks += 1
        track_times = []

        for segment in track.segments:
            track_times.extend(_collect_point_times(segment.points))

        track_start = min(track_times) if track_times else None
        track_end = max(track_times) if track_times else None
        track_name = track.name or f"Track {track_index}"

        print(f"Track {track_index}: {track_name}")
        print(f"  start: {_fmt(track_start)}")
        print(f"  koniec: {_fmt(track_end)}")

        for segment_index, segment in enumerate(track.segments, start=1):
            total_segments += 1
            segment_times = _collect_point_times(segment.points)
            segment_start = min(segment_times) if segment_times else None
            segment_end = max(segment_times) if segment_times else None
            print(f"  Segment {segment_index}: start={_fmt(segment_start)}, koniec={_fmt(segment_end)}")

    print(f"[OK] Sprawdzono ścieżki: {total_tracks}, segmenty: {total_segments}")
    return gpx
