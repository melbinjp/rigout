"""Platform detection for the endpoint a tool is about to build a command for.

`TunnelManager.get_local_endpoint` stores `platform.system().lower()`, which is
`"darwin"` on macOS. Every tool that asked `"win" in endpoint.platform` therefore
treated a Mac as Windows: `"win" in "darwin"` is `True`. That produced PowerShell
commands on macOS, `if not exist` on /bin/sh and Chocolatey instead of Homebrew.

The predicates live here, once, and match whole tokens rather than substrings.
`is_windows_platform` also refuses outright on any macOS token, so a new call site
that checks only for Windows still cannot fire on a Mac.
"""

import re

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

# "darwin" is what platform.system() reports; the rest are how a user might
# label an endpoint by hand in mcp-server-config.json.
_MACOS_TOKENS = frozenset({"darwin", "mac", "macos", "macosx", "osx", "apple"})
_WINDOWS_TOKENS = frozenset(
    {"windows", "win", "win32", "win64", "winnt", "cygwin", "msys", "mingw", "mingw32", "mingw64"}
)
_LINUX_TOKENS = frozenset(
    {"linux", "ubuntu", "debian", "centos", "rhel", "fedora", "arch", "alpine", "suse", "gnu"}
)

WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"


def platform_tokens(value: object) -> list[str]:
    """Split an endpoint platform label into lowercase alphanumeric tokens.

    `"Windows 11 Pro"` -> `["windows", "11", "pro"]`, `"Darwin"` -> `["darwin"]`.
    Anything that is not a usable string (an unset attribute, a test double)
    simply yields tokens that match nothing, which lands on the POSIX default.
    """
    if not isinstance(value, str):
        return []
    return _TOKEN_PATTERN.findall(value.lower())


def is_macos_platform(value: object) -> bool:
    """Report whether an endpoint platform label denotes macOS."""
    return any(token in _MACOS_TOKENS or token.startswith("macos") for token in platform_tokens(value))


def is_windows_platform(value: object) -> bool:
    """Report whether an endpoint platform label denotes Windows.

    macOS is excluded explicitly rather than by ordering, so this answer is
    correct on its own and does not depend on the caller testing macOS first.
    """
    tokens = platform_tokens(value)
    if any(token in _MACOS_TOKENS for token in tokens):
        return False
    return any(token in _WINDOWS_TOKENS or token.startswith("windows") for token in tokens)


def is_linux_platform(value: object) -> bool:
    """Report whether an endpoint platform label names a Linux system."""
    tokens = platform_tokens(value)
    if any(token in _MACOS_TOKENS or token in _WINDOWS_TOKENS for token in tokens):
        return False
    return any(token in _LINUX_TOKENS for token in tokens)


def platform_family(value: object) -> str:
    """Return `WINDOWS`, `MACOS` or `LINUX` for an endpoint platform label.

    Unknown labels fall back to `LINUX`, matching the POSIX branch these tools
    have always used as their default.
    """
    if is_macos_platform(value):
        return MACOS
    if is_windows_platform(value):
        return WINDOWS
    return LINUX
