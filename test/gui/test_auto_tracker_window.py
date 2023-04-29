import uuid
from unittest.mock import MagicMock

import pytest

from randovania.game_connection.game_connection import ConnectedGameState
from randovania.games.game import RandovaniaGame
from randovania.gui.auto_tracker_window import AutoTrackerWindow
from randovania.gui.widgets.item_tracker_widget import ItemTrackerWidget


def test_create_tracker_no_game(skip_qtbot):
    # Setup
    connection = MagicMock()
    connection.connected_states = {}
    connection.get_connector_for_builder.return_value = None

    connector = MagicMock()
    connector.game_enum = RandovaniaGame.METROID_PRIME

    # Run
    window = AutoTrackerWindow(connection, None, MagicMock())
    skip_qtbot.addWidget(window)
    # window.create_tracker() is implied

    # Assert
    assert window.item_tracker is None
    assert window._dummy_tracker is not None

    # Now swap to a proper tracker
    connection.get_connector_for_builder.return_value = connector
    window.create_tracker()

    assert window._dummy_tracker is None
    assert window._current_tracker_game == RandovaniaGame.METROID_PRIME


@pytest.mark.parametrize(
    ["game", "tracker_name"], [
        (RandovaniaGame.METROID_PRIME, "Game Art (Standard)"),
        (RandovaniaGame.METROID_PRIME_ECHOES, "Game Art (Standard)"),
    ])
def test_create_tracker_valid(skip_qtbot, game, tracker_name):
    # Setup
    connection = MagicMock()
    connector = connection.get_connector_for_builder.return_value
    connector.game_enum = game
    connection.connected_states = {
        connector: ConnectedGameState(uuid.UUID(int=0), connector)
    }

    # Run
    window = AutoTrackerWindow(connection, None, MagicMock())
    skip_qtbot.addWidget(window)
    # window.create_tracker() is implied

    # Assert
    assert isinstance(window.item_tracker, ItemTrackerWidget)
    assert len(window.item_tracker.tracker_elements) > 10
    assert window._dummy_tracker is None
    assert window._current_tracker_game == game

    # Select new theme
    action = [action for action in window._tracker_actions[game]
              if action.text() == tracker_name][0]
    action.setChecked(True)

    window.create_tracker()
    assert window._current_tracker_name == tracker_name
