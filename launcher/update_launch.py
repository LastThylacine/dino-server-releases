"""Windows process launch used by the self-updater."""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path
from typing import Callable, Sequence


ShellExecute = Callable[[object, str, str, str, str, int], int]


def launch_update_helper(
    command: Sequence[str],
    working_directory: Path,
    *,
    shell_execute: ShellExecute | None = None,
) -> None:
    """Launch an updater outside the PyInstaller process job.

    A normal detached ``Popen`` child is terminated when the one-file
    PyInstaller bootloader exits on affected Windows systems. ShellExecute
    delegates process creation to the Windows shell broker, so the helper
    survives long enough to replace and restart the launcher.
    """
    if not command:
        raise ValueError("The update helper command is empty.")

    executable = str(command[0])
    arguments = subprocess.list2cmdline([str(value) for value in command[1:]])
    working_directory = Path(working_directory).resolve()

    if os.name != "nt" and shell_execute is None:
        subprocess.Popen(
            [executable, *command[1:]],
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return

    invoke = shell_execute or ctypes.windll.shell32.ShellExecuteW
    result = int(
        invoke(
            None,
            "open",
            executable,
            arguments,
            str(working_directory),
            0,
        )
    )
    if result <= 32:
        raise OSError(
            result,
            f"Windows could not launch the update helper (ShellExecute {result}).",
        )
