from typing import Any

from PySide6 import QtWidgets

from randovania.gui.tracker.tracker_state import TrackerState
from randovania.resolver.state import State


class TrackerComponent(QtWidgets.QDockWidget):
    def reset(self):
        raise NotImplementedError()

    def decode_persisted_state(self, previous_state: dict) -> Any | None:
        raise NotImplementedError()

    def apply_previous_state(self, previous_state: Any) -> None:
        raise NotImplementedError()

    def persist_current_state(self) -> dict:
        raise NotImplementedError()

    def fill_into_state(self, state: State):
        raise NotImplementedError()

    def tracker_update(self, tracker_state: TrackerState):
        pass
