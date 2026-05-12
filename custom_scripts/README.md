# GPXEditor Custom Scripts

The Custom Scripts panel loads only `.py` files that:

- do not start with `_`
- define a top-level `run(...)` function

Basic script shape:

```python
def run(gpx):
    for track in gpx.tracks:
        for segment in track.segments:
            segment.points = segment.points[::2]
```

Advanced script shape:

```python
def run(gpx, context):
    selected = context.selected_indices
    editor = context.editor
    current_path = context.current_path
```

Mutate the passed `gpx` object in place. After `run()` returns, GPXEditor rebuilds
the current view from that object. You can also return a replacement
`gpxpy.gpx.GPX` object.

Files without `run(...)` are treated as legacy standalone scripts and are not
shown as buttons in the editor.
