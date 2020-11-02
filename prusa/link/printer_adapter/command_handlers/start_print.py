import logging
from pathlib import Path

from prusa.connect.printer.const import State, Source
from prusa.link.printer_adapter.command import CommandHandler
from prusa.link.printer_adapter.default_settings import get_settings
from prusa.link.printer_adapter.informers.filesystem.models import \
    InternalFileTree
from prusa.link.printer_adapter.informers.state_manager import StateChange
from prusa.link.printer_adapter.structures.regular_expressions import \
    OPEN_RESULT_REGEX

LOG = get_settings().LOG


log = logging.getLogger(__name__)
log.setLevel(LOG.COMMANDS)


class StartPrint(CommandHandler):
    command_name = "start print"

    def _run_command(self):
        # No new print jobs while already printing
        # or when there is an Error/Attention state
        if self.state_manager.printing_state is not None:
            self.failed("Already printing")
            return

        if self.state_manager.override_state is not None:
            self.failed(f"Cannot print in "
                        f"{self.state_manager.get_state()} state.")
            return
        # TODO: command_id retrieved in a really bad way
        self.state_manager.expect_change(
            StateChange(self.caller.command_id,
                        to_states={State.PRINTING: Source.CONNECT}))

        file_path_string = self.caller.args[0]
        path = Path(file_path_string)
        parts = path.parts
        log.info(parts)

        if parts[1] == "SD Card":
            # Cut the first "/" and "SD Card" off
            self._load_file(str(Path(*parts[2:])))
            self._start_print()
        else:
            self._start_file_print(str(path))

        self.state_manager.printing()
        self.state_manager.stop_expecting_change()

    def _start_file_print(self, path):
        file_to_print: InternalFileTree = self.model.file_tree.get_file(path)
        os_path = file_to_print.full_fs_path

        self.file_printer.print(os_path)

    def _load_file(self, raw_path):
        file_name = raw_path.lower()

        instruction = self.do_matchable(f"M23 {file_name}", OPEN_RESULT_REGEX)
        match = instruction.match()

        if not match or match.groups()[0] is None:  # Opening failed
            self.failed(f"Wrong file name, or bad file. File name: {file_name}")

    def _start_print(self):
        self.do_instruction("M24")
