from datetime import timedelta

from PySide6 import QtCore, QtWidgets


TITLE = "Set Exact Track Start Time"
DISPLAY_FORMAT = "yyyy-MM-dd HH:mm:ss"


def _collect_timed_points(gpx):
    timed_points = []
    for track in getattr(gpx, "tracks", []):
        for segment in track.segments:
            for point in segment.points:
                if getattr(point, "time", None) is not None:
                    timed_points.append(point)
    return timed_points


def _first_timestamp(points):
    return min(point.time for point in points)


def _build_dialog(parent, current_start):
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle(TITLE)
    dialog.setModal(True)

    layout = QtWidgets.QVBoxLayout(dialog)

    info_label = QtWidgets.QLabel(
        "Set the exact start date and time for the track.\n"
        "All existing timestamps will be shifted by the same amount."
    )
    info_label.setWordWrap(True)
    layout.addWidget(info_label)

    current_label = QtWidgets.QLabel(
        f"Current start: {current_start.isoformat(sep=' ', timespec='seconds')}"
    )
    layout.addWidget(current_label)

    datetime_edit = QtWidgets.QDateTimeEdit(dialog)
    datetime_edit.setDisplayFormat(DISPLAY_FORMAT)
    datetime_edit.setCalendarPopup(True)
    datetime_edit.setTimeSpec(QtCore.Qt.TimeSpec.UTC)
    qdate = QtCore.QDate(
        current_start.year,
        current_start.month,
        current_start.day,
    )
    qtime = QtCore.QTime(
        current_start.hour,
        current_start.minute,
        current_start.second,
    )
    datetime_edit.setDateTime(
        QtCore.QDateTime(qdate, qtime, QtCore.Qt.TimeSpec.UTC)
    )
    layout.addWidget(datetime_edit)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.StandardButton.Ok
        | QtWidgets.QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    return dialog, datetime_edit


def _resolve_dialog_parent(context):
    editor_window = getattr(getattr(context, "editor", None), "canvas", None)
    if isinstance(editor_window, QtWidgets.QWidget):
        return editor_window.window()

    active_window = QtWidgets.QApplication.activeWindow()
    if isinstance(active_window, QtWidgets.QWidget):
        return active_window

    return None


def run(gpx, context):
    timed_points = _collect_timed_points(gpx)
    if not timed_points:
        raise RuntimeError("This GPX does not contain timestamps.")

    current_start = _first_timestamp(timed_points)
    dialog_parent = _resolve_dialog_parent(context)
    dialog, datetime_edit = _build_dialog(dialog_parent, current_start)

    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        print("[i] Operation cancelled.")
        return gpx

    selected_dt = datetime_edit.dateTime().toPython()
    if selected_dt.tzinfo is None and current_start.tzinfo is not None:
        selected_dt = selected_dt.replace(tzinfo=current_start.tzinfo)

    delta = selected_dt - current_start
    if delta == timedelta(0):
        print("[i] Start time unchanged.")
        return gpx

    for point in timed_points:
        point.time = point.time + delta

    new_start = _first_timestamp(timed_points)
    print(
        "[OK] Track start time updated: "
        f"{current_start.isoformat(sep=' ', timespec='seconds')} -> "
        f"{new_start.isoformat(sep=' ', timespec='seconds')}"
    )
    return gpx
