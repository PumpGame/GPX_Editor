from datetime import timedelta


SHIFT = timedelta(hours=1)


def _shift_time_attr(obj):
    timestamp = getattr(obj, "time", None)
    if timestamp is None:
        return 0

    obj.time = timestamp + SHIFT
    return 1


def run(gpx):
    """
    Shift all GPX timestamps forward by 1 hour.

    The script only updates `time` attributes and leaves all other GPX data
    unchanged.
    """
    shifted = 0

    shifted += _shift_time_attr(gpx)

    for waypoint in getattr(gpx, "waypoints", []):
        shifted += _shift_time_attr(waypoint)

    for route in getattr(gpx, "routes", []):
        shifted += _shift_time_attr(route)
        for point in getattr(route, "points", []):
            shifted += _shift_time_attr(point)

    for track in getattr(gpx, "tracks", []):
        shifted += _shift_time_attr(track)
        for segment in getattr(track, "segments", []):
            shifted += _shift_time_attr(segment)
            for point in getattr(segment, "points", []):
                shifted += _shift_time_attr(point)

    print(f"[OK] Shifted {shifted} timestamp(s) forward by 1 hour.")
    return gpx
