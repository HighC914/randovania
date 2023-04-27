import collections
import functools
import json
import math
import typing
from pathlib import Path
from typing import Iterator

from PySide6 import QtWidgets, QtGui, QtCore
from PySide6.QtCore import Qt

from randovania.game_description.game_description import GameDescription
from randovania.game_description.requirements.base import Requirement
from randovania.game_description.requirements.requirement_and import RequirementAnd
from randovania.game_description.requirements.resource_requirement import ResourceRequirement
from randovania.game_description.resources.pickup_entry import PickupEntry
from randovania.game_description.world.area import Area
from randovania.game_description.world.area_identifier import AreaIdentifier
from randovania.game_description.world.configurable_node import ConfigurableNode
from randovania.game_description.world.node import Node
from randovania.game_description.world.node_identifier import NodeIdentifier
from randovania.game_description.world.resource_node import ResourceNode
from randovania.game_description.world.teleporter_node import TeleporterNode
from randovania.game_description.world.world import World
from randovania.games.game import RandovaniaGame
from randovania.games.prime2.layout import translator_configuration
from randovania.games.prime2.layout.echoes_configuration import EchoesConfiguration
from randovania.games.prime2.layout.translator_configuration import LayoutTranslatorRequirement
from randovania.generator import generator
from randovania.gui.dialog.scroll_label_dialog import ScrollLabelDialog
from randovania.gui.generated.tracker_window_ui import Ui_TrackerWindow
from randovania.gui.lib import signal_handling
from randovania.gui.lib.common_qt_lib import set_default_window_icon
from randovania.gui.lib.scroll_protected import ScrollProtectedSpinBox
from randovania.gui.tracker.tracker_component import TrackerComponent
from randovania.gui.tracker.tracker_elevators import TrackerElevatorsWidget
from randovania.gui.tracker.tracker_pickup_inventory import TrackerPickupInventory
from randovania.gui.tracker.tracker_translators import TrackerTranslatorsWidget
from randovania.layout.base.base_configuration import BaseConfiguration
from randovania.layout.lib.teleporters import TeleporterShuffleMode, TeleporterConfiguration
from randovania.layout.preset import Preset
from randovania.layout.versioned_preset import InvalidPreset, VersionedPreset
from randovania.patching.prime import elevators
from randovania.resolver.logic import Logic
from randovania.resolver.resolver_reach import ResolverReach
from randovania.resolver.state import State, add_pickup_to_state


class InvalidLayoutForTracker(Exception):
    pass


def _persisted_preset_path(persistence_path: Path) -> Path:
    return persistence_path.joinpath(f"preset.{VersionedPreset.file_extension()}")


def _load_previous_state(persistence_path: Path,
                         game_configuration: BaseConfiguration,
                         ) -> dict | None:
    previous_layout_path = _persisted_preset_path(persistence_path)
    try:
        previous_configuration = VersionedPreset.from_file_sync(previous_layout_path).get_preset().configuration
    except (FileNotFoundError, json.JSONDecodeError, InvalidPreset):
        return None

    if previous_configuration != game_configuration:
        return None

    previous_state_path = persistence_path.joinpath("state.json")
    try:
        with previous_state_path.open() as previous_state_file:
            return json.load(previous_state_file)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


class TrackerWindow(QtWidgets.QMainWindow, Ui_TrackerWindow):
    # Tracker state
    _collected_pickups: dict[PickupEntry, int]
    _actions: list[Node]

    # Tracker configuration
    logic: Logic
    game_description: GameDescription
    game_configuration: BaseConfiguration
    persistence_path: Path
    tracker_components: list[TrackerComponent]
    _initial_state: State
    _starting_nodes: set[ResourceNode]

    # UI tools
    _world_name_to_item: dict[str, QtWidgets.QTreeWidgetItem]
    _area_name_to_item: dict[tuple[str, str], QtWidgets.QTreeWidgetItem]
    _node_to_item: dict[Node, QtWidgets.QTreeWidgetItem]
    _widget_for_pickup: dict[PickupEntry, QtWidgets.QCheckBox | QtWidgets.QComboBox]
    _during_setup = False

    @classmethod
    async def create_new(cls, persistence_path: Path, preset: Preset) -> "TrackerWindow":
        result = cls(persistence_path, preset)
        await result.configure()
        return result

    def __init__(self, persistence_path: Path, preset: Preset):
        super().__init__()
        self.setupUi(self)
        set_default_window_icon(self)

        self._collected_pickups = {}
        self._widget_for_pickup = {}
        self._actions = []
        self._world_name_to_item = {}
        self._area_name_to_item = {}
        self._node_to_item = {}
        self.preset = preset
        self.game_configuration = preset.configuration
        self.persistence_path = persistence_path
        self.tracker_components = []

    async def configure(self):
        player_pool = await generator.create_player_pool(None, self.game_configuration, 0, 1, rng_required=False)
        pool_patches = player_pool.patches

        bootstrap = self.game_configuration.game.generator.bootstrap

        self.game_description, self._initial_state = bootstrap.logic_bootstrap(
            self.preset.configuration,
            player_pool.game,
            pool_patches,
        )
        self.logic = Logic(self.game_description, self.preset.configuration)
        self.map_canvas.select_game(self.game_description.game)

        self._initial_state.resources.add_self_as_requirement_to_resources = True

        self.menu_reset_action.triggered.connect(self._confirm_reset)
        self.resource_filter_check.stateChanged.connect(self.update_locations_tree_for_reachable_nodes)
        self.hide_collected_resources_check.stateChanged.connect(self.update_locations_tree_for_reachable_nodes)
        self.undo_last_action_button.clicked.connect(self._undo_last_action)

        self.configuration_label.setText("Trick Level: {}; Starts with:\n{}".format(
            self.preset.configuration.trick_level.pretty_description(self.game_description),
            ", ".join(
                resource.short_name
                for resource, _ in pool_patches.starting_resources().as_resource_gain()
            )
        ))

        self.setup_possible_locations_tree()

        for it in [TrackerPickupInventory, TrackerElevatorsWidget, TrackerTranslatorsWidget]:
            component = it.create_for(player_pool, self.game_configuration)
            if component is not None:
                self.tracker_components.append(component)
                self.addDockWidget(QtCore.Qt.DockWidgetArea.TopDockWidgetArea, component)

        for first, second in zip(self.tracker_components[:-1], self.tracker_components[1:]):
            self.tabifyDockWidget(first, second)

        # Map
        for world in sorted(self.game_description.world_list.worlds, key=lambda x: x.name):
            self.map_world_combo.addItem(world.name, userData=world)

        self.on_map_world_combo(0)
        self.map_world_combo.currentIndexChanged.connect(self.on_map_world_combo)
        self.map_area_combo.currentIndexChanged.connect(self.on_map_area_combo)
        self.map_canvas.set_edit_mode(False)
        self.map_canvas.SelectAreaRequest.connect(self.focus_on_area)

        # Graph Map
        from randovania.gui.tracker.tracker_graph_map import MatplotlibWidget
        self.matplot_widget = MatplotlibWidget(self.tab_graph_map, self.game_description.world_list)
        self.tab_graph_map_layout.addWidget(self.matplot_widget)
        self.map_tab_widget.currentChanged.connect(self._on_tab_changed)

        for world in self.game_description.world_list.worlds:
            self.graph_map_world_combo.addItem(world.name, world)
        self.graph_map_world_combo.currentIndexChanged.connect(self.on_graph_map_world_combo)

        self.persistence_path.mkdir(parents=True, exist_ok=True)
        previous_state = _load_previous_state(self.persistence_path, self.preset.configuration)

        if not self.apply_previous_state(previous_state):
            self.setup_starting_location(None)

            VersionedPreset.with_preset(self.preset).save_to_file(
                _persisted_preset_path(self.persistence_path)
            )
            self._add_new_action(self._initial_state.node)

    def apply_previous_state(self, previous_state: dict | None) -> bool:
        if previous_state is None:
            return False

        starting_location = None
        needs_starting_location = len(self.game_configuration.starting_location.locations) > 1

        try:
            previous_actions = [
                self.game_description.world_list.node_by_identifier(
                    NodeIdentifier.from_string(identifier)
                )
                for identifier in previous_state["actions"]
            ]
            if needs_starting_location:
                starting_location = AreaIdentifier.from_json(previous_state["starting_location"])

        except (KeyError, AttributeError):
            return False

        restored_states = [
            component.decode_persisted_state(previous_state)
            for component in self.tracker_components
        ]
        if None in restored_states:
            return False

        self.setup_starting_location(starting_location)

        for component, restored_state in zip(self.tracker_components, restored_states):
            component.apply_previous_state(restored_state)

        self._add_new_actions(previous_actions)

        world_list = self.game_description.world_list
        state = self.state_for_current_configuration()
        self.focus_on_world(world_list.nodes_to_world(state.node))
        self.focus_on_area(world_list.nodes_to_area(state.node))

        return True

    def reset(self):
        for component in self.tracker_components:
            component.reset()

        while len(self._actions) > 1:
            self._actions.pop()
            self.actions_list.takeItem(len(self._actions))

        self._refresh_for_new_action()

    def _confirm_reset(self):
        buttons = QtWidgets.QMessageBox.StandardButton

        reply = QtWidgets.QMessageBox.question(self, "Reset Tracker?", "Do you want to reset the tracker progression?",
                                               buttons.Yes | buttons.No, buttons.No)
        if reply == buttons.Yes:
            self.reset()

    @property
    def _show_only_resource_nodes(self) -> bool:
        return self.resource_filter_check.isChecked()

    @property
    def _hide_collected_resources(self) -> bool:
        return self.hide_collected_resources_check.isChecked()

    @property
    def _collected_nodes(self) -> set[ResourceNode]:
        return self._starting_nodes | {action for action in self._actions if action.is_resource_node}

    def _pretty_node_name(self, node: Node) -> str:
        world_list = self.game_description.world_list
        return f"{world_list.area_name(world_list.nodes_to_area(node))} / {node.name}"

    def _refresh_for_new_action(self):
        self.undo_last_action_button.setEnabled(len(self._actions) > 1)
        self.current_location_label.setText(f"Current location: {self._pretty_node_name(self._actions[-1])}")
        self.update_locations_tree_for_reachable_nodes()

    def _add_new_action(self, node: Node):
        self._add_new_actions([node])

    def _add_new_actions(self, nodes: Iterator[Node]):
        for node in nodes:
            self.actions_list.addItem(self._pretty_node_name(node))
            self._actions.append(node)
        self._refresh_for_new_action()

    def _undo_last_action(self):
        self._actions.pop()
        self.actions_list.takeItem(len(self._actions))
        self._refresh_for_new_action()

    def _on_tree_node_double_clicked(self, item: QtWidgets.QTreeWidgetItem, _):
        node: Node | None = getattr(item, "node", None)

        if not item.isDisabled() and node is not None and node != self._actions[-1]:
            self._add_new_action(node)

    def _on_show_path_to_here(self):
        target: QtWidgets.QTreeWidgetItem = self.possible_locations_tree.currentItem()
        if target is None:
            return
        node: Node | None = getattr(target, "node", None)
        if node is not None:
            reach = ResolverReach.calculate_reach(self.logic, self.state_for_current_configuration())
            try:
                path = reach.path_to_node(node)
            except KeyError:
                path = []

            wl = self.logic.game.world_list
            text = [f"<p><span style='font-weight:600;'>Path to {node.name}</span></p><ul>"]
            for p in path:
                text.append(f"<li>{wl.node_name(p, with_world=True, distinguish_dark_aether=True)}</li>")
            text.append("</ul>")

            dialog = ScrollLabelDialog("".join(text), "Path to node", self)
            dialog.exec_()
        else:
            QtWidgets.QMessageBox.warning(self, "Invalid target",
                                          f"Can't find a path to {target.text(0)}. Please select a node.")

    # Map

    def on_map_world_combo(self, _):
        world: World = self.map_world_combo.currentData()
        self.map_area_combo.clear()
        for area in sorted(world.areas, key=lambda x: x.name):
            self.map_area_combo.addItem(area.name, userData=area)

        self.map_canvas.select_world(world)
        self.on_map_area_combo(0)

    def on_map_area_combo(self, _):
        area: Area = self.map_area_combo.currentData()
        self.map_canvas.select_area(area)

    # Graph Map

    def update_matplot_widget(self, nodes_in_reach: set[Node]):
        self.matplot_widget.update_for(
            self.graph_map_world_combo.currentData(),
            self.state_for_current_configuration(),
            nodes_in_reach,
        )

    def on_graph_map_world_combo(self):
        nodes_in_reach = self.current_nodes_in_reach(self.state_for_current_configuration())
        self.update_matplot_widget(nodes_in_reach)

    def current_nodes_in_reach(self, state: State | None):
        if state is None:
            nodes_in_reach = set()
        else:
            reach = ResolverReach.calculate_reach(self.logic, state)
            nodes_in_reach = set(reach.nodes)
            nodes_in_reach.add(state.node)
        return nodes_in_reach

    def _on_tab_changed(self):
        if self.map_tab_widget.currentWidget() == self.tab_graph_map:
            self.on_graph_map_world_combo()

    def update_locations_tree_for_reachable_nodes(self):
        state = self.state_for_current_configuration()
        context = state.node_context()
        nodes_in_reach = self.current_nodes_in_reach(state)

        if self.map_tab_widget.currentWidget() == self.tab_graph_map:
            self.update_matplot_widget(nodes_in_reach)

        for world in self.game_description.world_list.worlds:
            for area in world.areas:
                area_is_visible = False
                for node in area.nodes:
                    is_visible = node in nodes_in_reach

                    node_item = self._node_to_item[node]
                    if node.is_resource_node:
                        resource_node = typing.cast(ResourceNode, node)

                        if self._show_only_resource_nodes:
                            is_visible = is_visible and not isinstance(node, ConfigurableNode)

                        is_collected = resource_node.is_collected(context)
                        is_visible = is_visible and not (self._hide_collected_resources and is_collected)

                        node_item.setDisabled(not resource_node.can_collect(context))
                        node_item.setCheckState(0, QtCore.Qt.Checked if is_collected else QtCore.Qt.Unchecked)

                    elif self._show_only_resource_nodes:
                        is_visible = False

                    node_item.setHidden(not is_visible)
                    area_is_visible = area_is_visible or is_visible
                self._area_name_to_item[(world.name, area.name)].setHidden(not area_is_visible)

        self.map_canvas.set_state(state)
        self.map_canvas.set_visible_nodes({
            node
            for node in nodes_in_reach
            if not self._node_to_item[node].isHidden()
        })

        # Persist the current state
        self.persist_current_state()

    def persist_current_state(self):
        world_list = self.game_description.world_list
        with self.persistence_path.joinpath("state.json").open("w") as state_file:
            json.dump(
                {
                    "actions": [
                        node.identifier.as_string
                        for node in self._actions
                    ],
                    "starting_location": world_list.identifier_for_node(self._initial_state.node
                                                                        ).area_identifier.as_json,
                },
                state_file
            )

    def setup_possible_locations_tree(self):
        """
        Creates the possible_locations_tree with all worlds, areas and nodes.
        """
        self.action_show_path_to_here = QtGui.QAction("Show path to here")
        self.action_show_path_to_here.triggered.connect(self._on_show_path_to_here)
        self.possible_locations_tree.itemDoubleClicked.connect(self._on_tree_node_double_clicked)
        self.possible_locations_tree.insertAction(None, self.action_show_path_to_here)

        # TODO: Dark World names
        for world in self.game_description.world_list.worlds:
            world_item = QtWidgets.QTreeWidgetItem(self.possible_locations_tree)
            world_item.setText(0, world.name)
            world_item.setExpanded(True)
            self._world_name_to_item[world.name] = world_item

            for area in world.areas:
                area_item = QtWidgets.QTreeWidgetItem(world_item)
                area_item.area = area
                area_item.setText(0, area.name)
                area_item.setHidden(True)
                self._area_name_to_item[(world.name, area.name)] = area_item

                for node in area.nodes:
                    node_item = QtWidgets.QTreeWidgetItem(area_item)
                    node_item.setText(0, node.name)
                    node_item.node = node
                    if node.is_resource_node:
                        node_item.setFlags(node_item.flags() & ~Qt.ItemIsUserCheckable)
                    self._node_to_item[node] = node_item

    def setup_starting_location(self, area_location: AreaIdentifier | None):
        world_list = self.game_description.world_list

        if len(self.game_configuration.starting_location.locations) > 1:
            if area_location is None:
                area_locations = sorted(self.game_configuration.starting_location.locations,
                                        key=lambda it: world_list.area_name(world_list.area_by_area_location(it)))

                location_names = [world_list.area_name(world_list.area_by_area_location(it))
                                  for it in area_locations]
                selected_name = QtWidgets.QInputDialog.getItem(self, "Starting Location", "Select starting location",
                                                               location_names, 0, False)
                area_location = area_locations[location_names.index(selected_name[0])]

            self._initial_state.node = world_list.resolve_teleporter_connection(area_location)

        def is_resource_node_present(node: Node, state: State):
            if node.is_resource_node:
                assert isinstance(node, ResourceNode)
                is_resource_set = self._initial_state.resources.is_resource_set
                return all(
                    is_resource_set(resource)
                    for resource, _ in node.resource_gain_on_collect(state.node_context())
                )
            return False

        self._starting_nodes = {
            node
            for node in world_list.iterate_nodes()
            if is_resource_node_present(node, self._initial_state)
        }

    def state_for_current_configuration(self) -> State | None:
        state = self._initial_state.copy()
        if self._actions:
            state.node = self._actions[-1]

        for component in self.tracker_components:
            state = component.fill_into_state(state)

        for pickup, quantity in self._collected_pickups.items():
            for _ in range(quantity):
                add_pickup_to_state(state, pickup)

        for node in self._collected_nodes:
            state.resources.add_resource_gain(
                node.resource_gain_on_collect(state.node_context())
            )

        return state

    # View
    def focus_on_world(self, world: World):
        signal_handling.set_combo_with_value(self.map_world_combo, world)
        signal_handling.set_combo_with_value(self.graph_map_world_combo, world)
        self.on_map_world_combo(0)

    def focus_on_area(self, area: Area):
        signal_handling.set_combo_with_value(self.map_area_combo, area)
