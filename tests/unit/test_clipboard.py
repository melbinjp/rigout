"""The clipboard helper is a convenience, so its contract is mostly about failing well.

Two properties matter more than the copying itself: a missing or broken clipboard tool
must never raise into a starting server, and the copied text must never travel as a
process argument, because `ps` shows arguments to every local user and the text being
copied can fetch a bearer token.
"""

import subprocess
from unittest.mock import patch

import pytest

from rigout import clipboard

SETUP_URL = "https://example.trycloudflare.com/connection.json?setup_token=TOKEN123"


@pytest.mark.unit
def test_copy_passes_the_text_on_stdin_and_never_as_an_argument():
    """Process arguments are world-readable through `ps`; the setup URL is a credential."""
    with (
        patch.object(clipboard.shutil, "which", return_value="/usr/bin/xclip"),
        patch.object(clipboard.subprocess, "run") as run,
    ):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        tool = clipboard.copy_to_clipboard(SETUP_URL)

    assert tool is not None
    argv = run.call_args.args[0]
    assert SETUP_URL not in " ".join(argv)
    assert run.call_args.kwargs["input"] == SETUP_URL.encode("utf-8")


@pytest.mark.unit
def test_copy_reports_none_when_no_clipboard_tool_exists():
    """Headless containers - the case that motivated this - have no clipboard at all."""
    with patch.object(clipboard.shutil, "which", return_value=None):
        assert clipboard.copy_to_clipboard(SETUP_URL) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "failure",
    [
        OSError("no such executable"),
        subprocess.TimeoutExpired(cmd="xclip", timeout=5),
        subprocess.SubprocessError("broken pipe"),
    ],
)
def test_copy_never_raises_when_the_clipboard_tool_misbehaves(failure):
    """A clipboard failure must not interrupt a server that is otherwise starting."""
    with (
        patch.object(clipboard.shutil, "which", return_value="/usr/bin/xclip"),
        patch.object(clipboard.subprocess, "run", side_effect=failure),
    ):
        assert clipboard.copy_to_clipboard(SETUP_URL) is None


@pytest.mark.unit
def test_copy_reports_none_when_the_tool_exits_nonzero():
    with (
        patch.object(clipboard.shutil, "which", return_value="/usr/bin/xclip"),
        patch.object(clipboard.subprocess, "run") as run,
    ):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
        assert clipboard.copy_to_clipboard(SETUP_URL) is None


@pytest.mark.unit
def test_copy_times_out_rather_than_hanging_a_start():
    """`xclip` without -selection can block holding the selection; a start must not."""
    with (
        patch.object(clipboard.shutil, "which", return_value="/usr/bin/xclip"),
        patch.object(clipboard.subprocess, "run") as run,
    ):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        clipboard.copy_to_clipboard(SETUP_URL)

    assert run.call_args.kwargs["timeout"] == clipboard.COPY_TIMEOUT_SECONDS


@pytest.mark.unit
def test_every_platform_has_at_least_one_candidate_tool():
    for platform, candidates in clipboard.CLIPBOARD_TOOLS.items():
        assert candidates, f"{platform} lists no clipboard tool"
        for candidate in candidates:
            assert candidate and isinstance(candidate[0], str)


@pytest.mark.unit
def test_linux_prefers_wayland_then_x11_then_the_wsl_bridge():
    """Order is the contract: a Wayland session usually also has xclip installed and broken."""
    names = [candidate[0] for candidate in clipboard.CLIPBOARD_TOOLS["linux"]]
    assert names.index("wl-copy") < names.index("xclip") < names.index("clip.exe")


@pytest.mark.unit
def test_first_available_tool_wins():
    with patch.object(
        clipboard.shutil, "which", side_effect=lambda name: None if name == "wl-copy" else f"/bin/{name}"
    ):
        command = clipboard.clipboard_command()

    # On any platform, the returned command must be one that `which` claimed to find.
    assert command is not None
    assert command[0] != "wl-copy"
