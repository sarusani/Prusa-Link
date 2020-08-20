"""Functions for gathering telemetry"""

import logging
import re
from threading import Thread

from old_buddy.informers.state_manager import StateManager
from old_buddy.structures.model_classes import Telemetry, States
from old_buddy.input_output.serial import Serial
from old_buddy.input_output.serial_queue.serial_queue import SerialQueue
from old_buddy.input_output.serial_queue.helpers import enqueue_list_from_str, \
    wait_for_instruction
from old_buddy.settings import QUIT_INTERVAL, TELEMETRY_INTERVAL, \
    TELEMETRY_GATHERER_LOG_LEVEL
from old_buddy.structures.regular_expressions import TEMPERATURE_REGEX, \
    POSITION_REGEX, E_FAN_REGEX, P_FAN_REGEX, PRINT_TIME_REGEX, \
    PROGRESS_REGEX, TIME_REMAINING_REGEX, HEATING_REGEX, HEATING_HOTEND_REGEX
from old_buddy.util import run_slowly_die_fast

# XXX:  "M221", "M220
TELEMETRY_GCODES = ["M105", "M114", "PRUSA FAN", "M27", "M73"]

log = logging.getLogger(__name__)
log.setLevel(TELEMETRY_GATHERER_LOG_LEVEL)


class TelemetryGatherer:

    def __init__(self, serial: Serial, serial_queue: SerialQueue):
        self.serial = serial
        self.serial_queue = serial_queue

        # Looked better wrapped to 120 characters. Just saying
        self.serial.register_output_handler(TEMPERATURE_REGEX,
                                            self.temperature_handler)
        self.serial.register_output_handler(POSITION_REGEX,
                                            self.position_handler)
        self.serial.register_output_handler(E_FAN_REGEX,
                                            self.fan_extruder_handler)
        self.serial.register_output_handler(P_FAN_REGEX,
                                            self.fan_print_handler)
        self.serial.register_output_handler(PRINT_TIME_REGEX,
                                            self.print_time_handler)
        self.serial.register_output_handler(PROGRESS_REGEX,
                                            self.progress_handler)
        self.serial.register_output_handler(TIME_REMAINING_REGEX,
                                            self.time_remaining_handler)
        self.serial.register_output_handler(HEATING_REGEX,
                                            self.heating_handler)
        self.serial.register_output_handler(HEATING_HOTEND_REGEX,
                                            self.heating_hotend_handler)

        StateManager.state_changed.connect(self.state_changed_handler)

        self.current_telemetry = Telemetry()
        self.last_telemetry = self.current_telemetry
        self.running = True
        self.polling_thread = Thread(target=self.keep_polling_telemetry,
                                     name="telemetry_polling_thread")
        self.polling_thread.start()

    def keep_polling_telemetry(self):
        run_slowly_die_fast(lambda: self.running, QUIT_INTERVAL,
                            TELEMETRY_INTERVAL, self.poll_telemetry)

    def poll_telemetry(self):
        instruction_list = enqueue_list_from_str(self.serial_queue,
                                                 TELEMETRY_GCODES)

        # Only ask for telemetry again, when the previous is confirmed
        for instruction in instruction_list:
            # Wait indefinitely, if the queue got stuck
            # we aren't the ones who should handle that
            wait_for_instruction(instruction, lambda: self.running)

    def temperature_handler(self, match: re.Match):
        groups = match.groups()
        self.current_telemetry.temp_nozzle = float(groups[0])
        self.current_telemetry.target_nozzle = float(groups[1])
        self.current_telemetry.temp_bed = float(groups[2])
        self.current_telemetry.target_bed = float(groups[3])

    def position_handler(self, match: re.Match):
        groups = match.groups()
        self.current_telemetry.axis_x = float(groups[4])
        self.current_telemetry.axis_y = float(groups[5])
        self.current_telemetry.axis_z = float(groups[6])

    def fan_extruder_handler(self, match: re.Match):
        self.current_telemetry.fan_extruder = float(match.groups()[0])

    def fan_print_handler(self, match: re.Match):
        self.current_telemetry.fan_print = float(match.groups()[0])

    def print_time_handler(self, match: re.Match):
        groups = match.groups()
        if groups[1] != "" and groups[1] is not None:
            printing_time_hours = int(groups[2])
            printing_time_mins = int(groups[3])
            hours_in_sec = printing_time_hours * 60 * 60
            mins_in_sec = printing_time_mins * 60
            printing_time_sec = mins_in_sec + hours_in_sec
            self.current_telemetry.time_printing = printing_time_sec

    def progress_handler(self, match: re.Match):
        groups = match.groups()
        progress = int(groups[0])
        if 0 <= progress <= 100:
            self.current_telemetry.progress = progress

    def time_remaining_handler(self, match: re.Match):
        # FIXME: Using the more conservative values from silent mode,
        #  need to know in which mode we are
        groups = match.groups()
        mins_remaining = int(groups[1])
        secs_remaining = mins_remaining * 60
        if mins_remaining >= 0:
            self.current_telemetry.time_estimated = secs_remaining

    def flow_rate_handler(self, match: re.Match):
        groups = match.groups()
        flow = int(groups[0])
        if 0 <= flow <= 100:
            self.current_telemetry.flow = flow

    def speed_multiplier_handler(self, match: re.Match):
        groups = match.groups()
        speed = int(groups[0])
        if 0 <= speed <= 100:
            self.current_telemetry.speed = speed

    def heating_handler(self, match: re.Match):
        groups = match.groups()

        self.current_telemetry.temp_nozzle = float(groups[0])
        self.current_telemetry.temp_bed = float(groups[1])

    def heating_hotend_handler(self, match: re.Match):
        groups = match.groups()

        self.current_telemetry.temp_nozzle = float(groups[0])

    def state_changed_handler(self, sender, command_id=None, source=None):
        # Some state changes can imply telemetry data.
        # For example, if we were not printing and now we are,
        # we have been printing for 0 min and we have 0% done
        if (sender.current_state == States.PRINTING and
                sender.last_state in {States.READY, States.BUSY}):
            self.current_telemetry.progress = 0
            self.current_telemetry.printing_time = 0

    def get_telemetry(self):
        """Returns telemetry gathered so far and starts anew"""
        # Actually returning last telemetry,
        # The current one will be constructed while we are busy
        # answering the telemetry response
        # FIXME: Should I change the timestamp?
        telemetry_to_return = self.last_telemetry

        self.last_telemetry = self.current_telemetry
        self.current_telemetry = Telemetry()

        return telemetry_to_return

    def stop(self):
        self.running = False
        self.polling_thread.join()