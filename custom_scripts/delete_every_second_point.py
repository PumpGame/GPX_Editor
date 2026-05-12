def run(gpx):
    """
    Deletes every second point from the GPX track.

    Args:
        gpx (gpxpy.gpx.GPX): The loaded GPX object.
    """
    if not gpx.tracks:
        print("[!] No tracks found in the GPX file.")
        return

    removed = 0
    for track in gpx.tracks:
        for segment in track.segments:
            before = len(segment.points)
            segment.points = [point for i, point in enumerate(segment.points) if i % 2 == 0]
            removed += before - len(segment.points)

    print(f"[OK] Deleted every second point from the GPX track. Removed: {removed}")
