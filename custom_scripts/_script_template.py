"""
Template for GPXEditor custom scripts.

Files that start with "_" are ignored by the Custom Scripts panel.
Copy this file, rename it without the leading "_", then edit run().
"""


def run(gpx):
    """
    Basic API.

    GPXEditor passes the currently loaded gpxpy.gpx.GPX object.
    Mutate it in place; the editor will rebuild the visible track after run()
    returns.
    """
    for track in gpx.tracks:
        for segment in track.segments:
            # Example: keep every second point.
            segment.points = segment.points[::2]


def run_with_context_example(gpx, context):
    """
    Optional advanced API example.

    If your script defines run(gpx, context), GPXEditor passes extra state:
    - context.editor: the GPXEditor instance
    - context.selected_indices: selected point indices from the current view
    - context.current_path: source GPX path, if known
    - context.numpy and context.gpxpy: imported helper modules
    """
    selected = set(context.selected_indices)
    if not selected:
        return

    # Example: delete currently selected indices from every segment that matches.
    for track in gpx.tracks:
        for segment in track.segments:
            segment.points = [
                point
                for index, point in enumerate(segment.points)
                if index not in selected
            ]
