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
                            self.event_queue.put(("operation_finished", axis))
                            self.event_queue.put(("operation_failed", str(exc)))
                            continue
                        if complete:
                            self.active_operations.pop(axis, None)
                            self.event_queue.put(("operation_finished", axis))
                    else:
                        status = self.controller.get_status(axis)
                    self.event_queue.put(("status", status))
            except Exception as exc:
                self._stop_timer()
                self._close_controller()
                self.event_queue.put(("connection_lost", str(exc)))

        def _start_move(self, axis, value, velocity, acceleration, *, relative):
            if self.controller is None:
                self.event_queue.put(("operation_failed", "Controller is not connected"))
                return
            parsed = Axis.parse(axis)
            try:
                method = (
                    self.controller.start_move_relative
                    if relative else self.controller.start_move_absolute
                )
                status = method(
                    parsed, value,
                    velocity_mm_s=velocity,
                    acceleration_mm_s2=acceleration,
                )
                self.active_operations[parsed] = "move"
                self.event_queue.put(("operation_started", (parsed, "move")))
                self.event_queue.put(("status", status))
            except Exception as exc:
                self.event_queue.put(("operation_finished", parsed))
                self.event_queue.put(("operation_failed", str(exc)))

        @QtCore.Slot(str, float, float, float)
        def move_absolute(self, axis, position_mm, velocity_mm_s, acceleration_mm_s2):
            self._start_move(
                axis, position_mm, velocity_mm_s, acceleration_mm_s2, relative=False
            )

        @QtCore.Slot(str, float, float, float)
        def move_relative(self, axis, distance_mm, velocity_mm_s, acceleration_mm_s2):
            self._start_move(
                axis, distance_mm, velocity_mm_s, acceleration_mm_s2, relative=True
            )

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

    class AxisPanel(QtWidgets.QGroupBox):
        absolute_requested = QtCore.Signal(str, float, float, float)
        relative_requested = QtCore.Signal(str, float, float, float)
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

            self.step = QtWidgets.QDoubleSpinBox()
            self.step.setDecimals(1)
            self.step.setRange(0.1, 100000.0)
            self.step.setValue(1.0)
            self.step.setSuffix(" µm")
            self.minus_button = QtWidgets.QPushButton("−")
            self.plus_button = QtWidgets.QPushButton("+")
            self.minus_button.clicked.connect(lambda: self._relative(-1))
            self.plus_button.clicked.connect(lambda: self._relative(1))
            self.save_default_button = QtWidgets.QPushButton("Save as Default")
            self.restore_default_button = QtWidgets.QPushButton("Restore Default")
            self.save_default_button.clicked.connect(
                lambda: self.save_default_requested.emit(self.axis)
            )
            self.restore_default_button.clicked.connect(
                lambda: self.restore_default_requested.emit(self.axis)
            )
            for field in (self.velocity, self.acceleration, self.step):
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
            move_layout.addWidget(QtWidgets.QLabel("Relative step"), 4, 0)
            move_layout.addWidget(self.step, 4, 1, 1, 2)
            move_layout.addWidget(self.minus_button, 5, 1)
            move_layout.addWidget(self.plus_button, 5, 2)
            defaults_row = QtWidgets.QHBoxLayout()
            defaults_row.addWidget(self.save_default_button)
            defaults_row.addWidget(self.restore_default_button)
            move_layout.addLayout(defaults_row, 6, 0, 1, 3)
            move_layout.setRowStretch(7, 1)

            self.jog_mode = QtWidgets.QComboBox()
            self.jog_mode.addItem("Single Step", "single_step")
            self.jog_step_size = QtWidgets.QDoubleSpinBox()
            self.jog_step_size.setDecimals(1)
            self.jog_step_size.setRange(0.1, 100000.0)
            self.jog_step_size.setValue(1.0)
            self.jog_step_size.setSuffix(" µm")
            self.jog_velocity = QtWidgets.QDoubleSpinBox()
            self.jog_velocity.setDecimals(4)
            self.jog_velocity.setMinimum(0.0001)
            self.jog_velocity.setSuffix(" mm/s")
            self.jog_acceleration = QtWidgets.QDoubleSpinBox()
            self.jog_acceleration.setDecimals(3)
            self.jog_acceleration.setMinimum(0.001)
            self.jog_acceleration.setSuffix(" mm/s²")
            self.jog_reverse_button = QtWidgets.QPushButton("◀")
            self.jog_forward_button = QtWidgets.QPushButton("▶")
            self.jog_reverse_button.clicked.connect(lambda: self._jog("reverse"))
            self.jog_forward_button.clicked.connect(lambda: self._jog("forward"))
            self.save_jog_default_button = QtWidgets.QPushButton("Save as Default")
            self.restore_jog_default_button = QtWidgets.QPushButton("Restore Default")
            self.save_jog_default_button.clicked.connect(
                lambda: self.save_jog_default_requested.emit(self.axis)
            )
            self.restore_jog_default_button.clicked.connect(
                lambda: self.restore_jog_default_requested.emit(self.axis)
            )
            for field in (
                self.jog_mode,
                self.jog_step_size,
                self.jog_velocity,
                self.jog_acceleration,
            ):
                signal = (
                    field.currentIndexChanged
                    if field is self.jog_mode else field.valueChanged
                )
                signal.connect(lambda _value: self.settings_changed.emit(self.axis))

            jog_page = QtWidgets.QWidget()
            jog_layout = QtWidgets.QGridLayout(jog_page)
            jog_layout.addWidget(QtWidgets.QLabel("Mode"), 0, 0)
            jog_layout.addWidget(self.jog_mode, 0, 1, 1, 2)
            jog_layout.addWidget(QtWidgets.QLabel("Step size"), 1, 0)
            jog_layout.addWidget(self.jog_step_size, 1, 1, 1, 2)
            jog_layout.addWidget(QtWidgets.QLabel("Maximum velocity"), 2, 0)
            jog_layout.addWidget(self.jog_velocity, 2, 1, 1, 2)
            jog_layout.addWidget(QtWidgets.QLabel("Acceleration"), 3, 0)
            jog_layout.addWidget(self.jog_acceleration, 3, 1, 1, 2)
            jog_layout.addWidget(QtWidgets.QLabel("Execute"), 4, 0)
            jog_layout.addWidget(self.jog_reverse_button, 4, 1)
            jog_layout.addWidget(self.jog_forward_button, 4, 2)
            jog_defaults_row = QtWidgets.QHBoxLayout()
            jog_defaults_row.addWidget(self.save_jog_default_button)
            jog_defaults_row.addWidget(self.restore_jog_default_button)
            jog_layout.addLayout(jog_defaults_row, 5, 0, 1, 3)
            jog_layout.setRowStretch(6, 1)

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
            self.step.setMaximum(maximum_step_um)
            self.jog_step_size.setMaximum(maximum_step_um)
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
                QtCore.QSignalBlocker(self.step),
            ]
            self.velocity.setValue(settings.velocity_mm_s)
            self.acceleration.setValue(settings.acceleration_mm_s2)
            self.step.setValue(settings.relative_step_um)
            del blockers

        def current_settings(self):
            return GuiAxisSettings(
                velocity_mm_s=self.velocity.value(),
                acceleration_mm_s2=self.acceleration.value(),
                relative_step_um=self.step.value(),
            )

        def apply_jog_settings(self, settings):
            blockers = [
                QtCore.QSignalBlocker(self.jog_mode),
                QtCore.QSignalBlocker(self.jog_step_size),
                QtCore.QSignalBlocker(self.jog_velocity),
                QtCore.QSignalBlocker(self.jog_acceleration),
            ]
            index = self.jog_mode.findData(settings.mode)
            self.jog_mode.setCurrentIndex(max(index, 0))
            self.jog_step_size.setValue(settings.step_size_um)
            self.jog_velocity.setValue(settings.velocity_mm_s)
            self.jog_acceleration.setValue(settings.acceleration_mm_s2)
            del blockers

        def current_jog_settings(self):
            return GuiJogSettings(
                mode=self.jog_mode.currentData(),
                step_size_um=self.jog_step_size.value(),
                velocity_mm_s=self.jog_velocity.value(),
                acceleration_mm_s2=self.jog_acceleration.value(),
            )

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
            move_enabled = self.connected and not self.motion_active
            for widget in (
                self.target, self.velocity, self.acceleration, self.move_button,
                self.step, self.minus_button, self.plus_button, self.enable_button,
                self.home_button, self.save_default_button, self.restore_default_button,
                self.jog_mode, self.jog_step_size, self.jog_velocity,
                self.jog_acceleration, self.jog_reverse_button,
                self.jog_forward_button, self.save_jog_default_button,
                self.restore_jog_default_button,
            ):
                widget.setEnabled(move_enabled)
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

        def _relative(self, direction):
            distance = direction * self.step.value() / 1000.0
            if self._confirm_distance(distance):
                velocity, acceleration = self._parameters()
                self.relative_requested.emit(self.axis, distance, velocity, acceleration)

        def _jog(self, direction):
            distance = self.jog_step_size.value() / 1000.0
            if self._confirm_distance(distance):
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
        relative_requested = QtCore.Signal(str, float, float, float)
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
                panel.relative_requested.connect(self.relative_requested)
                panel.jog_requested.connect(self.jog_requested)
                panel.stop_requested.connect(self.stop_requested)
                panel.enabled_requested.connect(self.enabled_requested)
                panel.home_requested.connect(self.home_requested)
                panel.settings_changed.connect(self._schedule_state_save)
                panel.save_default_requested.connect(self._save_default)
                panel.restore_default_requested.connect(self._restore_default)
                panel.save_jog_default_requested.connect(self._save_jog_default)
                panel.restore_jog_default_requested.connect(self._restore_jog_default)
            layout = QtWidgets.QVBoxLayout()
            layout.addLayout(top)
            layout.addLayout(axes_layout, 1)
            central = QtWidgets.QWidget()
            central.setLayout(layout)
            self.setCentralWidget(central)
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
                elif kind == "operation_started":
                    axis, operation = payload
                    self.panels[axis].set_operation(operation)
                elif kind == "operation_finished":
                    self.panels[payload].set_operation(None)
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

        @QtCore.Slot(object)
        def on_status(self, status):
            if not self.shutting_down:
                self.panels[status.axis].update_status(status)

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
    window.relative_requested.connect(worker.move_relative, queued)
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
