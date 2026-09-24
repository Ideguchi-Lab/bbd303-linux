"""Small PySide6 control panel for the BBD303."""

from queue import Empty, SimpleQueue
import sys

_CONFIRM_DISTANCE_MM = 1.0


def _pyside6():
    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError as exc:
        raise RuntimeError(
            "PySide6 is not installed. Run: conda activate bbd && "
            "python -m pip install -e '.[gui]'"
        ) from exc
    return QtCore, QtWidgets


def main(argv: list[str] | None = None) -> int:
    try:
        QtCore, QtWidgets = _pyside6()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    from .controller import BBD303Controller
    from .errors import DeviceStateError, MotionTimeoutError
    from .gui_state import GuiAxisSettings, GuiJogSettings, GuiStateStore
    from .models import Axis

    class ControllerWorker(QtCore.QObject):
        finished = QtCore.Signal()

        def __init__(self, event_queue):
            super().__init__()
            self.event_queue = event_queue
            self.controller = None
            self.timer = None
            self.active_operations = {}
            self.position_route = None
            self.shutting_down = False

        def _stop_timer(self):
            if self.timer is not None:
                self.timer.stop()
                self.timer.deleteLater()
                self.timer = None

        def _close_controller(self):
            if self.controller is None:
                return
            try:
                self.controller.close()
            except Exception:
                pass
            finally:
                self.active_operations.clear()
                self.position_route = None
                self.controller = None

        @QtCore.Slot()
        def connect_device(self):
            if self.controller is not None or self.shutting_down:
                return
            controller = None
            try:
                controller = BBD303Controller()
                controller.connect()
                info = controller.get_device_info()
                axes = {
                    axis: (
                        controller.get_stage_info(axis),
                        controller.get_motion_parameters(axis),
                        controller.get_jog_parameters(axis),
                    )
                    for axis in Axis
                }
                self.controller = controller
                self.timer = QtCore.QTimer(self)
                self.timer.setInterval(200)
                self.timer.timeout.connect(self.poll)
                self.timer.start()
                self.event_queue.put(("connected", (info, axes)))
                self.poll()
            except Exception as exc:
                if controller is not None:
                    try:
                        controller.close()
                    except Exception:
                        pass
                self.event_queue.put(("connection_lost", str(exc)))

        @QtCore.Slot()
        def poll(self):
            if self.controller is None:
                return
            try:
                for axis in Axis:
                    operation = self.active_operations.get(axis)
                    if operation is not None:
                        try:
                            if operation == "home":
                                method = self.controller.poll_home
                            elif operation == "jog_step":
                                method = self.controller.poll_jog_step
                            else:
                                method = self.controller.poll_motion
                            status, complete = method(axis)
                        except (DeviceStateError, MotionTimeoutError) as exc:
                            self.active_operations.pop(axis, None)
                            if self.position_route is not None:
                                self.position_route = None
                                self.event_queue.put(("position_move_finished", None))
                            else:
                                self.event_queue.put(("operation_finished", axis))
                            self.event_queue.put(("operation_failed", str(exc)))
                            continue
                        if complete:
                            self.active_operations.pop(axis, None)
                            if self.position_route is not None:
                                try:
                                    self._start_next_position_axis()
                                except (DeviceStateError, MotionTimeoutError) as exc:
                                    self.position_route = None
                                    self.event_queue.put(("position_move_finished", None))
                                    self.event_queue.put(("operation_failed", str(exc)))
                            else:
                                self.event_queue.put(("operation_finished", axis))
                    else:
                        status = self.controller.get_status(axis)
                    self.event_queue.put(("status", status))
            except Exception as exc:
                self._stop_timer()
                self._close_controller()
                self.event_queue.put(("connection_lost", str(exc)))

        @QtCore.Slot(str, float, float, float)
        def move_absolute(self, axis, position_mm, velocity_mm_s, acceleration_mm_s2):
            if self.controller is None:
                self.event_queue.put(("operation_failed", "Controller is not connected"))
                return
            parsed = Axis.parse(axis)
            try:
                status = self.controller.start_move_absolute(
                    parsed, position_mm,
                    velocity_mm_s=velocity_mm_s,
                    acceleration_mm_s2=acceleration_mm_s2,
                )
                self.active_operations[parsed] = "move"
                self.event_queue.put(("operation_started", (parsed, "move")))
                self.event_queue.put(("status", status))
            except Exception as exc:
                self.event_queue.put(("operation_finished", parsed))
                self.event_queue.put(("operation_failed", str(exc)))

        def _start_next_position_axis(self):
            if self.position_route is None:
                return
            while self.position_route:
                axis, target, velocity, acceleration = self.position_route.pop(0)
                current = self.controller.get_status(axis)
                counts = self.controller.get_stage_info(axis).counts_per_mm
                if abs(current.position_counts - round(target * counts)) <= 4:
                    continue
                status = self.controller.start_move_absolute(
                    axis, target,
                    velocity_mm_s=velocity,
                    acceleration_mm_s2=acceleration,
                )
                self.active_operations[axis] = "position_move"
                self.event_queue.put(("status", status))
                return
            self.position_route = None
            self.event_queue.put(("position_move_finished", None))

        @QtCore.Slot(float, float, float, float, float, float)
        def move_to_saved_position(self, x, y, vx, ax, vy, ay):
            if self.controller is None:
                self.event_queue.put(("position_move_finished", None))
                self.event_queue.put(("operation_failed", "Controller is not connected"))
                return
            if self.position_route is not None or self.active_operations:
                self.event_queue.put(("position_move_finished", None))
                self.event_queue.put(("operation_failed", "Another operation is active"))
                return
            try:
                for axis, target in ((Axis.X, x), (Axis.Y, y)):
                    stage = self.controller.get_stage_info(axis)
                    status = self.controller.get_status(axis)
                    if not stage.min_position_mm < target < stage.max_position_mm:
                        raise DeviceStateError(
                            f"Saved {axis.value.upper()} target is outside stage travel"
                        )
                    self.controller._require_ready(status)
                self.position_route = [
                    (Axis.X, x, vx, ax), (Axis.Y, y, vy, ay)
                ]
                self.event_queue.put(("position_move_started", None))
                self._start_next_position_axis()
            except Exception as exc:
                self.position_route = None
                self.event_queue.put(("position_move_finished", None))
                self.event_queue.put(("operation_failed", str(exc)))

        @QtCore.Slot(str, str, float, float, float)
        def jog_step(
            self, axis, direction, step_size_mm, velocity_mm_s, acceleration_mm_s2
        ):
            if self.controller is None:
                self.event_queue.put(("operation_failed", "Controller is not connected"))
                return
            parsed = Axis.parse(axis)
            try:
                status = self.controller.start_jog_step(
                    parsed,
                    direction,
                    step_size_mm=step_size_mm,
                    velocity_mm_s=velocity_mm_s,
                    acceleration_mm_s2=acceleration_mm_s2,
                )
                self.active_operations[parsed] = "jog_step"
                self.event_queue.put(("operation_started", (parsed, "jog_step")))
                self.event_queue.put(("status", status))
            except Exception as exc:
                self.event_queue.put(("operation_finished", parsed))
                self.event_queue.put(("operation_failed", str(exc)))

        @QtCore.Slot(str)
        def home_axis(self, axis):
            if self.controller is None:
                self.event_queue.put(("operation_failed", "Controller is not connected"))
                return
            parsed = Axis.parse(axis)
            try:
                status = self.controller.start_home(parsed, confirmed=True, timeout_s=180.0)
                self.active_operations[parsed] = "home"
                self.event_queue.put(("operation_started", (parsed, "home")))
                self.event_queue.put(("status", status))
            except Exception as exc:
                self.event_queue.put(("operation_finished", parsed))
                self.event_queue.put(("operation_failed", str(exc)))

        @QtCore.Slot(str)
        def stop_axis(self, axis):
            if self.controller is None:
                return
            parsed = Axis.parse(axis)
            try:
                if self.position_route is not None:
                    active_axes = [
                        item for item, operation in self.active_operations.items()
                        if operation == "position_move"
                    ]
                    self.position_route = None
                    try:
                        for active_axis in active_axes:
                            self.controller.stop(active_axis)
                            self.active_operations.pop(active_axis, None)
                    finally:
                        self.event_queue.put(("position_move_finished", None))
                    return
                self.controller.stop(parsed)
                self.active_operations.pop(parsed, None)
                self.event_queue.put(("operation_finished", parsed))
                self.event_queue.put(("status", self.controller.get_status(parsed)))
            except Exception as exc:
                self.event_queue.put(("operation_failed", str(exc)))

        @QtCore.Slot(str, bool)
        def set_enabled(self, axis, enabled):
            if self.controller is None:
                return
            try:
                result = self.controller.enable(axis) if enabled else self.controller.disable(axis)
                self.event_queue.put(("status", result))
            except Exception as exc:
                self.event_queue.put(("operation_failed", str(exc)))

        @QtCore.Slot()
        def shutdown(self):
            if self.shutting_down:
                return
            self.shutting_down = True
            try:
                self._stop_timer()
                self._close_controller()
            finally:
                self.finished.emit()

    class Indicator(QtWidgets.QWidget):
        def __init__(self, text):
            super().__init__()
            self.dot = QtWidgets.QLabel("●")
            self.text = QtWidgets.QLabel(text)
            layout = QtWidgets.QHBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
            layout.addWidget(self.dot)
            layout.addWidget(self.text)
            layout.addStretch()
            self.set_state(False)

        def set_state(self, active, *, alert=False):
            color = "#c62828" if active and alert else "#159447" if active else "#8a8a8a"
            self.dot.setStyleSheet(f"color: {color};")

    class SortableTableItem(QtWidgets.QTableWidgetItem):
        def __init__(self, text, sort_value):
            super().__init__(text)
            self.sort_value = sort_value

        def __lt__(self, other):
            if isinstance(other, SortableTableItem):
                return self.sort_value < other.sort_value
            return super().__lt__(other)

    class AxisPanel(QtWidgets.QGroupBox):
        absolute_requested = QtCore.Signal(str, float, float, float)
        jog_requested = QtCore.Signal(str, str, float, float, float)
        stop_requested = QtCore.Signal(str)
        enabled_requested = QtCore.Signal(str, bool)
        home_requested = QtCore.Signal(str)
        settings_changed = QtCore.Signal(str)
        save_default_requested = QtCore.Signal(str)
        restore_default_requested = QtCore.Signal(str)
        save_jog_default_requested = QtCore.Signal(str)
        restore_jog_default_requested = QtCore.Signal(str)

        def __init__(self, axis: Axis):
            super().__init__(f"{axis.value.upper()} axis")
            self.axis = axis.value
            self.last_position = None
            self.last_enabled = False
            self.connected = False
            self.motion_active = False

            self.position = QtWidgets.QLabel("--.----- mm")
            self.position.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            font = self.position.font()
            font.setPointSize(font.pointSize() + 14)
            font.setBold(True)
            self.position.setFont(font)
            self.forward_limit = Indicator("Forward Hard Limit")
            self.reverse_limit = Indicator("Reverse Hard Limit")
            self.homed = Indicator("Homed")
            self.normal = Indicator("Normal")

            self.enable_button = QtWidgets.QPushButton("Enable")
            self.enable_button.clicked.connect(self._toggle_enabled)
            self.home_button = QtWidgets.QPushButton("Home")
            self.home_button.clicked.connect(self._home)
            self.stop_button = QtWidgets.QPushButton("STOP")
            self.stop_button.setStyleSheet("font-weight: bold; color: #b00020;")
            self.stop_button.clicked.connect(lambda: self.stop_requested.emit(self.axis))

            self.target = QtWidgets.QDoubleSpinBox()
            self.target.setDecimals(5)
            self.target.setRange(-1000.0, 1000.0)
            self.target.setSuffix(" mm")
            self.velocity = QtWidgets.QDoubleSpinBox()
            self.velocity.setDecimals(4)
            self.velocity.setMinimum(0.0001)
            self.velocity.setSuffix(" mm/s")
            self.acceleration = QtWidgets.QDoubleSpinBox()
            self.acceleration.setDecimals(3)
            self.acceleration.setMinimum(0.001)
            self.acceleration.setSuffix(" mm/s²")
            self.move_button = QtWidgets.QPushButton("Execute")
            self.move_button.clicked.connect(self._absolute)

            self.save_default_button = QtWidgets.QPushButton("Save as Default")
            self.restore_default_button = QtWidgets.QPushButton("Restore Default")
            self.save_default_button.clicked.connect(
                lambda: self.save_default_requested.emit(self.axis)
            )
            self.restore_default_button.clicked.connect(
                lambda: self.restore_default_requested.emit(self.axis)
            )
            for field in (self.velocity, self.acceleration):
                field.valueChanged.connect(lambda _value: self.settings_changed.emit(self.axis))

            move_page = QtWidgets.QWidget()
            move_layout = QtWidgets.QGridLayout(move_page)
            move_layout.addWidget(QtWidgets.QLabel("Position"), 0, 0)
            move_layout.addWidget(self.target, 0, 1, 1, 2)
            move_layout.addWidget(QtWidgets.QLabel("Maximum velocity"), 1, 0)
            move_layout.addWidget(self.velocity, 1, 1, 1, 2)
            move_layout.addWidget(QtWidgets.QLabel("Acceleration"), 2, 0)
            move_layout.addWidget(self.acceleration, 2, 1, 1, 2)
            move_layout.addWidget(self.move_button, 3, 1, 1, 2)
            defaults_row = QtWidgets.QHBoxLayout()
            defaults_row.addWidget(self.save_default_button)
            defaults_row.addWidget(self.restore_default_button)
            move_layout.addLayout(defaults_row, 4, 0, 1, 3)
            move_layout.setRowStretch(5, 1)

            self.jog_mode = QtWidgets.QComboBox()
            self.jog_mode.addItem("Single Step", "single_step")
            self.jog_mode.addItem("Continuous (unavailable)", "continuous")
            self.jog_mode.model().item(1).setEnabled(False)
            self.jog_velocity = QtWidgets.QDoubleSpinBox()
            self.jog_velocity.setDecimals(4)
            self.jog_velocity.setMinimum(0.0001)
            self.jog_velocity.setSuffix(" mm/s")
            self.jog_acceleration = QtWidgets.QDoubleSpinBox()
            self.jog_acceleration.setDecimals(3)
            self.jog_acceleration.setMinimum(0.001)
            self.jog_acceleration.setSuffix(" mm/s²")
            self.jog_buttons = []
            self.jog_step_fields = []
            self.edit_steps_button = QtWidgets.QPushButton("Edit steps…")
            self.edit_steps_button.setCheckable(True)
            self.edit_steps_button.toggled.connect(self._toggle_step_editor)
            self.step_editor = QtWidgets.QWidget()
            editor_layout = QtWidgets.QGridLayout(self.step_editor)
            editor_layout.setContentsMargins(0, 0, 0, 0)
            for index, value in enumerate((1.0, 5.0, 10.0, 50.0)):
                field = QtWidgets.QDoubleSpinBox()
                field.setDecimals(2)
                field.setRange(0.05, 100000.0)
                field.setValue(value)
                field.setSuffix(" µm")
                field.valueChanged.connect(self._step_value_changed)
                self.jog_step_fields.append(field)
                editor_layout.addWidget(QtWidgets.QLabel(f"Step {index + 1}"), index, 0)
                editor_layout.addWidget(field, index, 1)
            self.step_editor.hide()
            self.save_jog_default_button = QtWidgets.QPushButton("Save as Default")
            self.restore_jog_default_button = QtWidgets.QPushButton("Restore Default")
            self.save_jog_default_button.clicked.connect(
                lambda: self.save_jog_default_requested.emit(self.axis)
            )
            self.restore_jog_default_button.clicked.connect(
                lambda: self.restore_jog_default_requested.emit(self.axis)
            )
            for field in (self.jog_velocity, self.jog_acceleration):
                field.valueChanged.connect(lambda _value: self.settings_changed.emit(self.axis))

            jog_page = QtWidgets.QWidget()
            jog_layout = QtWidgets.QGridLayout(jog_page)
            jog_layout.addWidget(QtWidgets.QLabel("Mode"), 0, 0)
            jog_layout.addWidget(self.jog_mode, 0, 1, 1, 2)
            jog_layout.addWidget(QtWidgets.QLabel("Maximum velocity"), 1, 0)
            jog_layout.addWidget(self.jog_velocity, 1, 1, 1, 2)
            jog_layout.addWidget(QtWidgets.QLabel("Acceleration"), 2, 0)
            jog_layout.addWidget(self.jog_acceleration, 2, 1, 1, 2)
            jog_layout.addWidget(QtWidgets.QLabel("Reverse  ◀"), 3, 0)
            jog_layout.addWidget(QtWidgets.QLabel("Forward  ▶"), 3, 2)
            for index in range(4):
                reverse = QtWidgets.QPushButton()
                forward = QtWidgets.QPushButton()
                reverse.clicked.connect(
                    lambda _checked=False, i=index: self._jog_step(i, "reverse")
                )
                forward.clicked.connect(
                    lambda _checked=False, i=index: self._jog_step(i, "forward")
                )
                self.jog_buttons.append((reverse, forward))
                jog_layout.addWidget(reverse, index + 4, 0)
                jog_layout.addWidget(QtWidgets.QLabel(f"Step {index + 1}"), index + 4, 1)
                jog_layout.addWidget(forward, index + 4, 2)
            self._update_jog_button_labels()
            jog_layout.addWidget(self.edit_steps_button, 8, 0, 1, 3)
            jog_layout.addWidget(self.step_editor, 9, 0, 1, 3)
            jog_defaults_row = QtWidgets.QHBoxLayout()
            jog_defaults_row.addWidget(self.save_jog_default_button)
            jog_defaults_row.addWidget(self.restore_jog_default_button)
            jog_layout.addLayout(jog_defaults_row, 10, 0, 1, 3)
            jog_layout.setRowStretch(11, 1)

            tabs = QtWidgets.QTabWidget()
            tabs.addTab(move_page, "Move")
            tabs.addTab(jog_page, "Jog")

            indicators = QtWidgets.QVBoxLayout()
            indicators.addWidget(self.forward_limit)
            indicators.addWidget(self.reverse_limit)
            indicators.addWidget(self.homed)
            indicators.addWidget(self.normal)
            position_row = QtWidgets.QHBoxLayout()
            position_row.addWidget(self.position, 1)
            position_row.addLayout(indicators)
            command_row = QtWidgets.QHBoxLayout()
            command_row.addWidget(self.enable_button)
            command_row.addWidget(self.home_button)
            command_row.addWidget(self.stop_button)

            layout = QtWidgets.QVBoxLayout(self)
            layout.addLayout(position_row)
            layout.addLayout(command_row)
            layout.addWidget(tabs, 1)
            self._update_controls()

        def configure(self, stage, settings, jog_settings):
            self.target.setRange(stage.min_position_mm, stage.max_position_mm)
            self.velocity.setRange(0.0001, stage.max_velocity_mm_s)
            self.acceleration.setRange(0.001, stage.max_acceleration_mm_s2)
            maximum_step_um = (
                stage.max_position_mm - stage.min_position_mm
            ) * 1000.0
            for field in self.jog_step_fields:
                field.setMaximum(maximum_step_um)
            self.jog_velocity.setRange(0.0001, stage.max_velocity_mm_s)
            self.jog_acceleration.setRange(0.001, stage.max_acceleration_mm_s2)
            self.apply_settings(settings)
            self.apply_jog_settings(jog_settings)
            self.connected = True
            self._update_controls()

        def apply_settings(self, settings):
            blockers = [
                QtCore.QSignalBlocker(self.velocity),
                QtCore.QSignalBlocker(self.acceleration),
            ]
            self.velocity.setValue(settings.velocity_mm_s)
            self.acceleration.setValue(settings.acceleration_mm_s2)
            del blockers

        def current_settings(self):
            return GuiAxisSettings(
                velocity_mm_s=self.velocity.value(),
                acceleration_mm_s2=self.acceleration.value(),
            )

        def apply_jog_settings(self, settings):
            fields = [self.jog_velocity, self.jog_acceleration, *self.jog_step_fields]
            blockers = [QtCore.QSignalBlocker(field) for field in fields]
            self.jog_mode.setCurrentIndex(0)
            self.jog_velocity.setValue(settings.velocity_mm_s)
            self.jog_acceleration.setValue(settings.acceleration_mm_s2)
            for field, value in zip(self.jog_step_fields, settings.steps_um):
                field.setValue(value)
            self._update_jog_button_labels()
            del blockers

        def current_jog_settings(self):
            return GuiJogSettings(
                mode="single_step",
                step_size_um=self.jog_step_fields[0].value(),
                velocity_mm_s=self.jog_velocity.value(),
                acceleration_mm_s2=self.jog_acceleration.value(),
                steps_um=tuple(field.value() for field in self.jog_step_fields),
            )

        def _toggle_step_editor(self, expanded):
            self.step_editor.setVisible(expanded)
            self.edit_steps_button.setText(
                "Hide step editor" if expanded else "Edit steps…"
            )

        def _step_value_changed(self, _value):
            self._update_jog_button_labels()
            self.settings_changed.emit(self.axis)

        def _update_jog_button_labels(self):
            for field, (reverse, forward) in zip(
                self.jog_step_fields, self.jog_buttons
            ):
                value = f"{field.value():g}"
                reverse.setText(f"−{value} µm")
                forward.setText(f"+{value} µm")

        def set_connected(self, connected):
            self.connected = connected
            if not connected:
                self.motion_active = False
            self._update_controls()

        def set_operation(self, operation):
            self.motion_active = operation is not None
            self.move_button.setText("Moving…" if operation == "move" else "Execute")
            self.home_button.setText("Homing…" if operation == "home" else "Home")
            self._update_controls()

        def _update_controls(self):
            idle = self.connected and not self.motion_active
            for widget in (
                self.target, self.velocity, self.acceleration, self.move_button,
                self.enable_button,
                self.home_button, self.save_default_button, self.restore_default_button,
                self.jog_mode, self.jog_velocity, self.jog_acceleration,
                self.edit_steps_button, self.save_jog_default_button,
                self.restore_jog_default_button, *self.jog_step_fields,
                *(button for pair in self.jog_buttons for button in pair),
            ):
                widget.setEnabled(idle)
            self.stop_button.setEnabled(self.connected)

        def update_status(self, status):
            first = self.last_position is None
            self.last_position = status.position_mm
            self.last_enabled = status.enabled
            self.position.setText(f"{status.position_mm:.5f} mm")
            if first:
                self.target.setValue(status.position_mm)
            self.forward_limit.set_state(status.forward_hard_limit, alert=True)
            self.reverse_limit.set_state(status.reverse_hard_limit, alert=True)
            self.homed.set_state(status.homed)
            self.normal.text.setText("Fault" if status.fault else "Normal")
            self.normal.set_state(True, alert=status.fault)
            self.enable_button.setText("Disable" if status.enabled else "Enable")

        def _parameters(self):
            return self.velocity.value(), self.acceleration.value()

        def _confirm_distance(self, distance_mm):
            if abs(distance_mm) <= _CONFIRM_DISTANCE_MM:
                return True
            answer = QtWidgets.QMessageBox.warning(
                self,
                "Confirm movement",
                f"This command may move {self.axis.upper()} by "
                f"{abs(distance_mm):.5f} mm. Verify the travel path is clear.",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            return answer == QtWidgets.QMessageBox.StandardButton.Yes

        @QtCore.Slot()
        def _absolute(self):
            target = self.target.value()
            distance = target - self.last_position if self.last_position is not None else 1e9
            if self._confirm_distance(distance):
                velocity, acceleration = self._parameters()
                self.absolute_requested.emit(self.axis, target, velocity, acceleration)

        def _jog_step(self, index, direction):
            if not self.connected or self.motion_active:
                return
            distance = self.jog_step_fields[index].value() / 1000.0
            if self._confirm_distance(distance):
                self.motion_active = True
                self._update_controls()
                self.jog_requested.emit(
                    self.axis,
                    direction,
                    distance,
                    self.jog_velocity.value(),
                    self.jog_acceleration.value(),
                )

        @QtCore.Slot()
        def _toggle_enabled(self):
            self.enabled_requested.emit(self.axis, not self.last_enabled)

        @QtCore.Slot()
        def _home(self):
            answer = QtWidgets.QMessageBox.warning(
                self,
                "Confirm Home",
                f"Homing {self.axis.upper()} may traverse the full mechanical range.\n"
                "Clear the entire travel path before continuing.",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                self.home_requested.emit(self.axis)

    class MainWindow(QtWidgets.QMainWindow):
        connect_requested = QtCore.Signal()
        shutdown_requested = QtCore.Signal()
        absolute_requested = QtCore.Signal(str, float, float, float)
        position_move_requested = QtCore.Signal(float, float, float, float, float, float)
        jog_requested = QtCore.Signal(str, str, float, float, float)
        stop_requested = QtCore.Signal(str)
        enabled_requested = QtCore.Signal(str, bool)
        home_requested = QtCore.Signal(str)

        def __init__(self, event_queue):
            super().__init__()
            self.event_queue = event_queue
            self.shutting_down = False
            self.may_close = False
            self.state_store = GuiStateStore()
            self.pending_state_axes = set()
            self.latest_status = {}
            self.position_move_active = False
            self.setWindowTitle("BBD303 Control")
            self.resize(920, 560)
            self.connection_label = QtWidgets.QLabel("Disconnected")
            self.connect_button = QtWidgets.QPushButton("Connect")
            self.connect_button.clicked.connect(self._connect)
            self.panels = {axis: AxisPanel(axis) for axis in Axis}

            top = QtWidgets.QHBoxLayout()
            top.addWidget(self.connection_label, 1)
            top.addWidget(self.connect_button)
            axes_layout = QtWidgets.QHBoxLayout()
            for panel in self.panels.values():
                axes_layout.addWidget(panel)
                panel.absolute_requested.connect(self.absolute_requested)
                panel.jog_requested.connect(self.jog_requested)
                panel.stop_requested.connect(self.stop_requested)
                panel.enabled_requested.connect(self.enabled_requested)
                panel.home_requested.connect(self.home_requested)
                panel.settings_changed.connect(self._schedule_state_save)
                panel.save_default_requested.connect(self._save_default)
                panel.restore_default_requested.connect(self._restore_default)
                panel.save_jog_default_requested.connect(self._save_jog_default)
                panel.restore_jog_default_requested.connect(self._restore_jog_default)
            control_page = QtWidgets.QWidget()
            control_page.setLayout(axes_layout)
            self.position_table = QtWidgets.QTableWidget(0, 4)
            self.position_table.setHorizontalHeaderLabels(
                ("Label", "X (mm)", "Y (mm)", "Saved at")
            )
            self.position_table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            )
            self.position_table.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
            )
            self.position_table.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            )
            position_header = self.position_table.horizontalHeader()
            position_header.setStretchLastSection(True)
            position_header.setSortIndicator(3, QtCore.Qt.SortOrder.DescendingOrder)
            self.position_table.setSortingEnabled(True)
            self.position_table.itemSelectionChanged.connect(self._update_position_buttons)
            self.save_position_button = QtWidgets.QPushButton("Save current")
            self.go_position_button = QtWidgets.QPushButton("Go")
            self.update_position_button = QtWidgets.QPushButton("Update selected")
            self.delete_position_button = QtWidgets.QPushButton("Delete")
            self.save_position_button.clicked.connect(self._save_position)
            self.go_position_button.clicked.connect(self._go_position)
            self.update_position_button.clicked.connect(self._update_position)
            self.delete_position_button.clicked.connect(self._delete_position)
            position_buttons = QtWidgets.QHBoxLayout()
            for button in (
                self.save_position_button, self.go_position_button,
                self.update_position_button, self.delete_position_button,
            ):
                position_buttons.addWidget(button)
            positions_page = QtWidgets.QWidget()
            positions_layout = QtWidgets.QVBoxLayout(positions_page)
            positions_layout.addWidget(self.position_table, 1)
            positions_layout.addLayout(position_buttons)
            self.pages = QtWidgets.QTabWidget()
            self.pages.addTab(control_page, "Control")
            self.pages.addTab(positions_page, "Positions")
            layout = QtWidgets.QVBoxLayout()
            layout.addLayout(top)
            layout.addWidget(self.pages, 1)
            central = QtWidgets.QWidget()
            central.setLayout(layout)
            self.setCentralWidget(central)
            self._refresh_positions()
            self._update_position_buttons()
            self.statusBar().showMessage("Ready")
            self.event_timer = QtCore.QTimer(self)
            self.event_timer.setInterval(50)
            self.event_timer.timeout.connect(self._drain_worker_events)
            self.event_timer.start()
            self.state_save_timer = QtCore.QTimer(self)
            self.state_save_timer.setSingleShot(True)
            self.state_save_timer.setInterval(300)
            self.state_save_timer.timeout.connect(self._flush_gui_state)
            if self.state_store.warning:
                self.statusBar().showMessage(self.state_store.warning, 10000)

        def _refresh_positions(self, select_id=None):
            if select_id is None:
                selected = self._selected_position()
                select_id = selected.id if selected is not None else None
            header = self.position_table.horizontalHeader()
            sort_column = header.sortIndicatorSection()
            sort_order = header.sortIndicatorOrder()
            self.position_table.setSortingEnabled(False)
            positions = self.state_store.positions
            self.position_table.setRowCount(len(positions))
            for row, position in enumerate(positions):
                timestamp = position.saved_at.replace("T", " ")[:19]
                items = (
                    SortableTableItem(position.label, position.label.casefold()),
                    SortableTableItem(f"{position.x_mm:.5f}", position.x_mm),
                    SortableTableItem(f"{position.y_mm:.5f}", position.y_mm),
                    SortableTableItem(timestamp, position.saved_at),
                )
                items[0].setData(QtCore.Qt.ItemDataRole.UserRole, position.id)
                for column, item in enumerate(items):
                    self.position_table.setItem(row, column, item)
            self.position_table.setSortingEnabled(True)
            self.position_table.sortItems(sort_column, sort_order)
            if select_id is not None:
                for row in range(self.position_table.rowCount()):
                    item = self.position_table.item(row, 0)
                    if item.data(QtCore.Qt.ItemDataRole.UserRole) == select_id:
                        self.position_table.selectRow(row)
                        break
            self._update_position_buttons()

        def _selected_position(self):
            row = self.position_table.currentRow()
            if row < 0:
                return None
            item = self.position_table.item(row, 0)
            if item is None:
                return None
            position_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
            return next(
                (item for item in self.state_store.positions if item.id == position_id),
                None,
            )

        def _stationary_coordinates(self):
            if self.position_move_active:
                return None
            statuses = [self.latest_status.get(axis) for axis in Axis]
            if any(status is None for status in statuses):
                return None
            if any(
                not status.enabled or not status.homed or not status.settled
                or status.moving or status.velocity_raw != 0 or status.fault
                for status in statuses
            ):
                return None
            if any(panel.motion_active for panel in self.panels.values()):
                return None
            return statuses[0].position_mm, statuses[1].position_mm

        def _update_position_buttons(self):
            selected = self._selected_position() is not None
            ready = not self.shutting_down and not self.position_move_active
            stationary = self._stationary_coordinates() is not None
            self.save_position_button.setEnabled(ready and stationary)
            self.go_position_button.setEnabled(ready and selected and stationary)
            self.update_position_button.setEnabled(ready and selected and stationary)
            self.delete_position_button.setEnabled(ready and selected)

        def _save_position(self):
            coordinates = self._stationary_coordinates()
            if coordinates is None:
                return
            label, accepted = QtWidgets.QInputDialog.getText(
                self, "Save position", "Label (optional):"
            )
            if not accepted:
                return
            try:
                position = self.state_store.add_position(label, *coordinates)
                self.state_store.save()
                self._refresh_positions(position.id)
            except (OSError, ValueError) as exc:
                self.on_error(f"Could not save position: {exc}")

        def _update_position(self):
            old = self._selected_position()
            coordinates = self._stationary_coordinates()
            if old is None or coordinates is None:
                return
            label, accepted = QtWidgets.QInputDialog.getText(
                self, "Update position", "Label (optional):", text=old.label
            )
            if not accepted:
                return
            try:
                position = self.state_store.update_position(
                    old.id, label, *coordinates
                )
                self.state_store.save()
                self._refresh_positions(position.id)
            except (OSError, ValueError) as exc:
                self.on_error(f"Could not update position: {exc}")

        def _delete_position(self):
            position = self._selected_position()
            if position is None:
                return
            answer = QtWidgets.QMessageBox.question(
                self, "Delete position",
                f"Delete {position.label or position.saved_at}?",
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            try:
                self.state_store.delete_position(position.id)
                self.state_store.save()
                self._refresh_positions()
            except (OSError, ValueError) as exc:
                self.on_error(f"Could not delete position: {exc}")

        def _go_position(self):
            position = self._selected_position()
            coordinates = self._stationary_coordinates()
            if position is None or coordinates is None:
                self.statusBar().showMessage("Both axes must be stationary and ready", 5000)
                return
            dx = position.x_mm - coordinates[0]
            dy = position.y_mm - coordinates[1]
            if max(abs(dx), abs(dy)) > _CONFIRM_DISTANCE_MM:
                answer = QtWidgets.QMessageBox.warning(
                    self, "Confirm saved position",
                    f"Move to {position.label or 'saved position'}?\n"
                    f"X: {coordinates[0]:.5f} → {position.x_mm:.5f} mm "
                    f"(Δ {dx:+.5f} mm)\n"
                    f"Y: {coordinates[1]:.5f} → {position.y_mm:.5f} mm "
                    f"(Δ {dy:+.5f} mm)\n"
                    "The axes will move sequentially: X, then Y. "
                    "Check the travel path is clear.",
                    QtWidgets.QMessageBox.StandardButton.Yes
                    | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
            x = self.panels[Axis.X].current_settings()
            y = self.panels[Axis.Y].current_settings()
            self.position_move_active = True
            for panel in self.panels.values():
                panel.set_operation("move")
            self._update_position_buttons()
            self.position_move_requested.emit(
                position.x_mm, position.y_mm,
                x.velocity_mm_s, x.acceleration_mm_s2,
                y.velocity_mm_s, y.acceleration_mm_s2,
            )

        @QtCore.Slot(str)
        def _schedule_state_save(self, axis):
            self.pending_state_axes.add(Axis.parse(axis))
            self.state_save_timer.start()

        @QtCore.Slot()
        def _flush_gui_state(self):
            if not self.pending_state_axes:
                return True
            axes = tuple(self.pending_state_axes)
            try:
                for axis in axes:
                    panel = self.panels[axis]
                    self.state_store.set_last(axis, panel.current_settings())
                    self.state_store.set_jog_last(axis, panel.current_jog_settings())
                self.state_store.save()
                self.pending_state_axes.difference_update(axes)
                return True
            except (OSError, ValueError) as exc:
                self.statusBar().showMessage(f"Could not save GUI settings: {exc}", 10000)
                return False

        @QtCore.Slot(str)
        def _save_default(self, axis):
            parsed = Axis.parse(axis)
            try:
                panel = self.panels[parsed]
                settings = panel.current_settings()
                self.state_store.set_defaults(parsed, settings)
                self.state_store.set_last(parsed, settings)
                self.state_store.set_jog_last(parsed, panel.current_jog_settings())
                self.state_store.save()
                self.pending_state_axes.discard(parsed)
                self.statusBar().showMessage(
                    f"Saved {parsed.value.upper()} settings as defaults", 5000
                )
            except (OSError, ValueError) as exc:
                self.statusBar().showMessage(f"Could not save GUI defaults: {exc}", 10000)

        @QtCore.Slot(str)
        def _restore_default(self, axis):
            parsed = Axis.parse(axis)
            self.panels[parsed].apply_settings(self.state_store.defaults(parsed))
            self.pending_state_axes.add(parsed)
            if self._flush_gui_state():
                self.statusBar().showMessage(
                    f"Restored {parsed.value.upper()} defaults", 5000
                )

        @QtCore.Slot(str)
        def _save_jog_default(self, axis):
            parsed = Axis.parse(axis)
            try:
                panel = self.panels[parsed]
                settings = panel.current_jog_settings()
                self.state_store.set_jog_defaults(parsed, settings)
                self.state_store.set_jog_last(parsed, settings)
                self.state_store.set_last(parsed, panel.current_settings())
                self.state_store.save()
                self.pending_state_axes.discard(parsed)
                self.statusBar().showMessage(
                    f"Saved {parsed.value.upper()} Jog settings as defaults", 5000
                )
            except (OSError, ValueError) as exc:
                self.statusBar().showMessage(f"Could not save Jog defaults: {exc}", 10000)

        @QtCore.Slot(str)
        def _restore_jog_default(self, axis):
            parsed = Axis.parse(axis)
            self.panels[parsed].apply_jog_settings(
                self.state_store.jog_defaults(parsed)
            )
            self.pending_state_axes.add(parsed)
            if self._flush_gui_state():
                self.statusBar().showMessage(
                    f"Restored {parsed.value.upper()} Jog defaults", 5000
                )

        @QtCore.Slot()
        def _drain_worker_events(self):
            while True:
                try:
                    kind, payload = self.event_queue.get_nowait()
                except Empty:
                    return
                if kind == "connected":
                    self.on_connected(*payload)
                elif kind == "status":
                    self.on_status(payload)
                elif kind == "position_move_started":
                    self.position_move_active = True
                    for panel in self.panels.values():
                        panel.set_operation("move")
                    self._update_position_buttons()
                elif kind == "position_move_finished":
                    self.position_move_active = False
                    for panel in self.panels.values():
                        panel.set_operation(None)
                    self._update_position_buttons()
                elif kind == "operation_started":
                    axis, operation = payload
                    self.panels[axis].set_operation(operation)
                elif kind == "operation_finished":
                    self.panels[payload].set_operation(None)
                    self._update_position_buttons()
                elif kind == "operation_failed":
                    self.on_error(payload)
                elif kind == "connection_lost":
                    self.on_connection_lost(payload)

        @QtCore.Slot()
        def _connect(self):
            if self.shutting_down:
                return
            self.connect_button.setEnabled(False)
            self.connection_label.setText("Connecting…")
            self.connect_requested.emit()

        @QtCore.Slot(object, object)
        def on_connected(self, info, axes):
            if self.shutting_down:
                return
            self.connection_label.setText(
                f"{info.model} | firmware {info.firmware} | {info.connection}"
            )
            for axis, panel in self.panels.items():
                stage, _motion, _jog = axes[axis]
                panel.configure(
                    stage,
                    self.state_store.last(axis),
                    self.state_store.jog_last(axis),
                )
            self.statusBar().showMessage("Connected")
            self._update_position_buttons()

        @QtCore.Slot(object)
        def on_status(self, status):
            if not self.shutting_down:
                self.latest_status[status.axis] = status
                self.panels[status.axis].update_status(status)
                self._update_position_buttons()

        @QtCore.Slot(str)
        def on_error(self, message):
            if self.shutting_down:
                return
            self.statusBar().showMessage(message, 10000)
            QtWidgets.QMessageBox.critical(self, "BBD303 error", message)

        @QtCore.Slot(str)
        def on_connection_lost(self, message):
            for panel in self.panels.values():
                panel.set_connected(False)
            self.latest_status.clear()
            self.position_move_active = False
            self._update_position_buttons()
            self.connection_label.setText("Disconnected")
            self.connect_button.setEnabled(not self.shutting_down)
            self.statusBar().showMessage(message, 10000)
            if not self.shutting_down:
                QtWidgets.QMessageBox.critical(self, "BBD303 connection error", message)

        @QtCore.Slot()
        def on_worker_thread_finished(self):
            self.may_close = True
            self.close()

        def closeEvent(self, event):
            if self.may_close:
                self._flush_gui_state()
                self.event_timer.stop()
                event.accept()
                return
            event.ignore()
            if not self.shutting_down:
                self.shutting_down = True
                self.connect_button.setEnabled(False)
                for panel in self.panels.values():
                    panel.set_connected(False)
                self.connection_label.setText("Closing…")
                self.statusBar().showMessage("Closing the XA connection…")
                self.shutdown_requested.emit()

    application = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    event_queue = SimpleQueue()
    window = MainWindow(event_queue)
    worker = ControllerWorker(event_queue)
    thread = QtCore.QThread()
    worker.moveToThread(thread)
    queued = QtCore.Qt.ConnectionType.QueuedConnection
    window.connect_requested.connect(worker.connect_device, queued)
    window.shutdown_requested.connect(worker.shutdown, queued)
    window.absolute_requested.connect(worker.move_absolute, queued)
    window.position_move_requested.connect(worker.move_to_saved_position, queued)
    window.jog_requested.connect(worker.jog_step, queued)
    window.stop_requested.connect(worker.stop_axis, queued)
    window.enabled_requested.connect(worker.set_enabled, queued)
    window.home_requested.connect(worker.home_axis, queued)
    worker.finished.connect(worker.deleteLater)
    worker.finished.connect(thread.quit)
    thread.finished.connect(window.on_worker_thread_finished, queued)
    thread.start()
    window.show()
    result = application.exec()
    if thread.isRunning():
        thread.quit()
        thread.wait()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
