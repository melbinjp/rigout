"""Copy text to the system clipboard using whatever tool the platform provides.

Rigout prints one URL that a human then has to move somewhere else. On a remote
container over SSH there is frequently one terminal, that terminal is occupied by the
running server, and Ctrl+C in it stops the server rather than copying anything. When a
clipboard tool exists, copying removes that step entirely.

Nothing here is required for Rigout to work. Every failure - no tool installed, no
display, a tool that exits nonzero - returns None, and the caller falls back to
printing the URL for manual selection.

The text is written to the tool's stdin and never passed as an argument, because
process arguments are readable by any local user through `ps`, and the value being
copied can fetch a bearer token.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

COPY_TIMEOUT_SECONDS = 5

# Ordered by preference within each platform. On Linux, Wayland's tool is tried before
# X11's, and clip.exe last so that WSL - where no native tool exists but the Windows
# clipboard is reachable - still works.
CLIPBOARD_TOOLS: dict[str, tuple[list[str], ...]] = {
    "win32": (["clip"],),
    "darwin": (["pbcopy"],),
    "linux": (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["clip.exe"],
    ),
}


def clipboard_command() -> list[str] | None:
    """Return the first clipboard command available here, or None if there is none."""
    candidates = CLIPBOARD_TOOLS.get(sys.platform, CLIPBOARD_TOOLS["linux"])
    for candidate in candidates:
        if shutil.which(candidate[0]):
            return list(candidate)
    return None


def copy_to_clipboard(text: str) -> str | None:
    """Copy `text`, returning the tool that accepted it, or None if it was not copied.

    Never raises: a clipboard is a convenience, and failing to reach one must not
    interrupt a server that is otherwise starting normally.
    """
    command = clipboard_command()
    if command is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, text goes in on stdin
            command,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=COPY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return command[0] if completed.returncode == 0 else None
