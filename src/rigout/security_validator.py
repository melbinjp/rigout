#!/usr/bin/env python3
"""
Security Validation Module for Enhanced MCP Hardware Server
Provides comprehensive security checks and validation
"""

import hashlib
import logging
import os
import posixpath
import re
import secrets
import shlex

logger = logging.getLogger(__name__)


class SecurityValidator:
    """Comprehensive security validation for MCP server operations"""

    # Prefix for every rejection that names a destructive construct.
    DANGEROUS_PREFIX = "Command contains dangerous pattern: "

    # Prefix for a rejection that came from the body of `sh -c`/`bash -c`. Kept
    # distinct so a nested rejection is not wrapped in DANGEROUS_PREFIX twice.
    NESTED_PREFIX = "Nested shell command rejected: "

    # Executables that are destructive whatever their arguments. DANGEROUS_PATTERNS
    # below catches the bare spellings; these catch the same commands when quoting
    # hides the name from a pattern that matches it (`"mkfs.ext4" /dev/sda1`).
    DESTRUCTIVE_EXECUTABLES = {
        "fdisk": "disk partitioning",
        "parted": "disk partitioning",
        "format": "filesystem format",
    }

    # Recursive force delete is catastrophic at the filesystem root and at the
    # top-level system directories, and ordinary work anywhere below them. The
    # `rm -rf /` regex this replaces could not tell those apart: it matched any
    # absolute path, so `rm -rf /tmp/build` was refused, while a quoted name or a
    # long flag (`'rm' -Rf /usr`) walked straight past it.
    PROTECTED_ROOTS = frozenset(
        {
            "/",
            "/bin",
            "/boot",
            "/dev",
            "/etc",
            "/home",
            "/lib",
            "/lib32",
            "/lib64",
            "/mnt",
            "/opt",
            "/proc",
            "/root",
            "/sbin",
            "/srv",
            "/sys",
            "/tmp",
            "/usr",
            "/var",
        }
    )

    # Raw disk devices and kernel memory. Unanchored so it can be searched inside a
    # `dd` operand as well as matched against a whole redirect target.
    RAW_DEVICE = re.compile(r"/dev/(?:sd[a-z]|hd[a-z]|nvme\w*|mmcblk\w*|mem\b|kmem|port)", re.IGNORECASE)

    # Dangerous command patterns that should be blocked or sanitized.
    #
    # `dd if=... of=...` is deliberately absent: having both flags is not what makes
    # a dd dangerous, naming a raw device is, and the flag-pair proxy refused
    # `dd if=/dev/urandom of=/tmp/testfile`. _semantic_danger checks the operands.
    #
    # `;\s*rm\s+`, `&&\s*rm\s+` and `\|\s*rm\s+` are deliberately absent too. They
    # blocked a chained `rm` that the identical unchained command was allowed to
    # run, so they taxed the operator rather than defending a boundary, and a
    # spurious refusal teaches callers to reach for bypass_security -- which
    # disables the checks that do defend one. Every destructive chain they caught
    # is caught by _semantic_danger, which splits at those operators already.
    DANGEROUS_PATTERNS = [
        r"mkfs\.",
        r"fdisk\s+",
        r"parted\s+",
        r"format\s+",
        r"del\s+/[sq]\s+",
        r"rmdir\s+/[sq]\s+",
        # Raw disk devices and kernel memory, in either redirect direction
        r"[<>]\s*/dev/(sd[a-z]|hd[a-z]|nvme\w*|mmcblk\w*|mem\b|kmem|port)",
        r"curl\s+.*\|\s*(ba)?sh\b",
        r"wget\s+.*\|\s*(ba)?sh\b",
        r"eval\s+\$\(",
        r"`[^`]*`",
        r"\$\([^)]*\)",
        r"nc\s+.*\s+\d+.*<",
        r"netcat\s+.*\s+\d+.*<",
    ]

    # What each pattern means, in the words a caller can act on. Without this the
    # refusal reads `Command contains dangerous pattern: \$\([^)]*\)`, which asks the
    # reader to parse a regex to find out what Rigout objected to, while every
    # _semantic_danger refusal already says something like "raw disk copy". The
    # substitution entries carry a way forward as well, because they are the two that
    # ordinary work runs into; without one, the only route the message offers is
    # bypass_security, which switches off every check including the ones that matter.
    DANGEROUS_PATTERN_REASONS = {
        r"mkfs\.": "filesystem creation",
        r"fdisk\s+": "disk partitioning",
        r"parted\s+": "disk partitioning",
        r"format\s+": "disk formatting",
        r"del\s+/[sq]\s+": "recursive or quiet Windows delete",
        r"rmdir\s+/[sq]\s+": "recursive or quiet Windows directory delete",
        r"[<>]\s*/dev/(sd[a-z]|hd[a-z]|nvme\w*|mmcblk\w*|mem\b|kmem|port)": (
            "redirection to or from a raw disk device or kernel memory"
        ),
        r"curl\s+.*\|\s*(ba)?sh\b": "a downloaded script piped straight into a shell",
        r"wget\s+.*\|\s*(ba)?sh\b": "a downloaded script piped straight into a shell",
        r"eval\s+\$\(": "eval of a command substitution",
        r"`[^`]*`": (
            "command substitution in backticks, whose contents Rigout cannot inspect. "
            "Run the inner command first and pass its output, or write it to a file and "
            "read that"
        ),
        r"\$\([^)]*\)": (
            "command substitution, whose contents Rigout cannot inspect. Run the inner "
            "command first and pass its output, or write it to a file and read that"
        ),
        r"nc\s+.*\s+\d+.*<": "netcat sending a file to a network address",
        r"netcat\s+.*\s+\d+.*<": "netcat sending a file to a network address",
    }

    # Allowed command prefixes for system operations
    ALLOWED_COMMANDS = [
        "ls",
        "cat",
        "grep",
        "find",
        "ps",
        "top",
        "htop",
        "df",
        "du",
        "free",
        "uname",
        "whoami",
        "id",
        "pwd",
        "cd",
        "mkdir",
        "touch",
        "cp",
        "mv",
        "echo",
        "printf",
        "env",
        "export",
        ".",
        "source",
        "bash",
        "sh",
        "chmod",
        "chown",
        "ln",
        "tar",
        "gzip",
        "gunzip",
        "zip",
        "unzip",
        "apt",
        "yum",
        "dnf",
        "pacman",
        "pip",
        "npm",
        "yarn",
        "docker",
        "git",
        "vim",
        "nano",
        "emacs",
        "python",
        "python3",
        "node",
        "java",
        "gcc",
        "make",
        "cmake",
        "systemctl",
        "service",
        "journalctl",
        "nvidia-smi",
        "lspci",
        "lsusb",
        "lscpu",
        "lsmem",
        "iostat",
        "vmstat",
        "head",
        "tail",
        "awk",
        "sed",
        "wc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "tee",
        "xargs",
        "which",
        "date",
        "hostname",
        "uptime",
        "nproc",
        "ss",
        "netstat",
        "ip",
        "ping",
        "sysctl",
        "wget",
        "curl",
        "conda",
        "cargo",
        "go",
        "rustc",
        "powershell",
        "sw_vers",
        "system_profiler",
        "vm_stat",
    ]

    def __init__(self):
        self.blocked_commands = []
        self.security_log = []

    @staticmethod
    def _mask_quoted_literals(command: str, *, include_double_quotes: bool) -> str:
        """Mask shell literals while preserving the command's character positions."""
        masked: list[str] = []
        quote: str | None = None
        escaped = False
        for char in command:
            if escaped:
                should_mask = quote == "'" or (quote == '"' and include_double_quotes)
                masked.append(" " if should_mask else char)
                escaped = False
                continue
            if char == "\\" and quote != "'":
                masked.append(" " if quote == '"' and include_double_quotes else char)
                escaped = True
                continue
            if quote:
                if char == quote:
                    quote = None
                    masked.append(" ")
                elif quote == "'" or include_double_quotes:
                    masked.append(" ")
                else:
                    masked.append(char)
                continue
            if char in {"'", '"'}:
                quote = char
                masked.append(" ")
                continue
            masked.append(char)
        return "".join(masked)

    @staticmethod
    def _tokenize_command(command: str) -> list[str]:
        """Tokenize shell operators without treating quoted operators as syntax."""
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.commenters = ""
        lexer.whitespace_split = True
        return list(lexer)

    @staticmethod
    def _command_segments(tokens: list[str]) -> list[list[str]]:
        """Split shell tokens at control operators while retaining redirections."""
        segments: list[list[str]] = [[]]
        for token in tokens:
            if token in {"&&", "||", ";", "|", "&"}:
                if segments[-1]:
                    segments.append([])
                continue
            segments[-1].append(token)
        return [segment for segment in segments if segment]

    @staticmethod
    def _pipelines(tokens: list[str]) -> list[list[list[str]]]:
        """Group tokens into pipelines, each one a list of pipe-separated segments."""
        pipelines: list[list[list[str]]] = [[[]]]
        for token in tokens:
            if token in {"&&", "||", ";", "&"}:
                pipelines.append([[]])
            elif token == "|":
                pipelines[-1].append([])
            else:
                pipelines[-1][-1].append(token)
        return pipelines

    @staticmethod
    def _segment_command(segment: list[str]) -> tuple[str, list[str]]:
        """Return the executable and arguments from one shell segment."""
        words = list(segment)
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words.pop(0)
        if not words:
            return "", []
        return os.path.basename(words[0]).lower(), words[1:]

    @classmethod
    def _is_protected_root(cls, target: str) -> bool:
        """Is this delete target the filesystem root or a top-level system directory?

        Paths below one of those are ordinary work (`/tmp/build`, `/var/log/app/old`)
        and stay available. posixpath is used explicitly because these are remote
        POSIX paths even when Rigout itself is running on Windows.
        """
        if target.startswith("/*"):
            return True
        return posixpath.normpath(target) in cls.PROTECTED_ROOTS

    def _semantic_danger(self, tokens: list[str]) -> str | None:
        """Detect destructive behavior that quote masking alone could hide.

        Returns a complete error message, already prefixed, or None. The message is
        built here rather than by the caller so a nested `sh -c` rejection can say
        that it was nested instead of being wrapped in the outer prefix a second time.
        """
        for index, token in enumerate(tokens[:-1]):
            if token in {">", ">>", "<", "<<"} and self.RAW_DEVICE.fullmatch(tokens[index + 1]):
                return f"{self.DANGEROUS_PREFIX}raw device redirection"

        # `curl ... | sh` stays dangerous when quoting hides either name. Only real
        # pipes count, so `curl -o setup.sh url && bash setup.sh` is unaffected.
        for pipeline in self._pipelines(tokens):
            names = [self._segment_command(segment)[0] for segment in pipeline]
            for index, name in enumerate(names):
                if name in {"curl", "wget"} and any(later in {"sh", "bash"} for later in names[index + 1 :]):
                    return f"{self.DANGEROUS_PREFIX}remote script piped to shell"

        segments = self._command_segments(tokens)
        for segment in segments:
            executable, args = self._segment_command(segment)
            if executable == "sudo" and args:
                executable = os.path.basename(args[0]).lower()
                args = args[1:]

            if executable == "mkfs" or executable.startswith("mkfs."):
                return f"{self.DANGEROUS_PREFIX}filesystem creation"

            if executable in self.DESTRUCTIVE_EXECUTABLES:
                return f"{self.DANGEROUS_PREFIX}{self.DESTRUCTIVE_EXECUTABLES[executable]}"

            # Both directions matter: `of=` a raw device destroys it, `if=` a raw
            # device copies a disk or kernel memory out to somewhere readable.
            if executable == "dd" and any(
                arg.startswith(("if=", "of=")) and self.RAW_DEVICE.search(arg) for arg in args
            ):
                return f"{self.DANGEROUS_PREFIX}raw disk copy"

            if executable in {"del", "rmdir"} and any(arg.lower() in {"/s", "/q"} for arg in args):
                return f"{self.DANGEROUS_PREFIX}recursive force delete"

            if executable == "rm":
                # -R and --recursive are the same flag as -r, so recognize every
                # spelling; the regex above only knows the literal `-rf`.
                short_flags = "".join(
                    arg[1:].lower() for arg in args if arg.startswith("-") and not arg.startswith("--")
                )
                long_flags = {arg.lower() for arg in args if arg.startswith("--")}
                recursive = "r" in short_flags or "--recursive" in long_flags
                force = "f" in short_flags or "--force" in long_flags
                targets = [arg for arg in args if not arg.startswith("-")]
                if recursive and force and any(self._is_protected_root(target) for target in targets):
                    return f"{self.DANGEROUS_PREFIX}recursive delete of a protected system directory"

            if executable in {"bash", "sh"} and len(args) >= 2 and args[0] == "-c":
                nested_safe, nested_error = self.validate_command(args[1], allow_sudo=True)
                if not nested_safe:
                    return f"{self.NESTED_PREFIX}{nested_error}"

        return None

    def validate_hostname(self, hostname: str) -> tuple[bool, str]:
        """
        Validate hostname for security and format compliance

        This is not the enforcement path. Every endpoint hostname is already checked
        by ``TunnelEndpoint._is_valid_hostname`` (ssh_manager.py) in ``__post_init__``,
        which rejects shell metacharacters explicitly and cannot be bypassed by
        constructing an endpoint. This method is the standalone equivalent, used by
        ``production_validation.py`` and by callers of the public ``SecurityValidator``.

        Args:
            hostname: The hostname to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not hostname or not isinstance(hostname, str):
            return False, "Hostname must be a non-empty string"

        # Length check
        if len(hostname) > 253:
            return False, "Hostname too long (max 253 characters)"

        # Format validation
        if hostname.startswith("-") or hostname.endswith("-"):
            return False, "Hostname cannot start or end with hyphen"

        if ".." in hostname:
            return False, "Hostname cannot contain consecutive dots"

        # Character validation
        allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
        if not all(c in allowed_chars for c in hostname):
            return False, "Hostname contains invalid characters"

        # Check for suspicious patterns
        suspicious_patterns = [
            r"localhost",
            r"127\.0\.0\.1",
            r"0\.0\.0\.0",
            r"::1",
            r".*\.local$",
            r".*\.internal$",
            r".*\.corp$",
        ]

        for pattern in suspicious_patterns:
            if re.match(pattern, hostname, re.IGNORECASE):
                logger.warning(f"Potentially suspicious hostname: {hostname}")

        return True, ""

    def validate_command(self, command: str, allow_sudo: bool = False) -> tuple[bool, str]:
        """
        Validate command for security risks

        Args:
            command: The command to validate
            allow_sudo: Whether sudo commands are allowed

        Returns:
            Tuple of (is_safe, error_message)
        """
        if not command or not isinstance(command, str):
            return False, "Command must be a non-empty string"

        # Remove leading/trailing whitespace
        command = command.strip()

        try:
            tokens = self._tokenize_command(command)
        except ValueError as exc:
            return False, f"Invalid shell syntax: {exc}"

        # Quoted text is data, not shell syntax. Command substitutions inside
        # double quotes remain executable, so mask only single quotes for them.
        unquoted_command = self._mask_quoted_literals(command, include_double_quotes=True)
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern in {r"`[^`]*`", r"\$\([^)]*\)"}:
                scan_text = self._mask_quoted_literals(command, include_double_quotes=False)
            else:
                scan_text = unquoted_command
            if re.search(pattern, scan_text, re.IGNORECASE):
                self.blocked_commands.append(command)
                reason = self.DANGEROUS_PATTERN_REASONS.get(pattern, pattern)
                return False, f"{self.DANGEROUS_PREFIX}{reason}"

        # _semantic_danger returns an already-formatted message; do not re-prefix it.
        semantic_danger = self._semantic_danger(tokens)
        if semantic_danger:
            self.blocked_commands.append(command)
            return False, semantic_danger

        # Inspect every segment of a chained/piped command the same way as the
        # main command: sudo is gated by allow_sudo, destructive patterns are
        # already blocked above, and unrecognized commands are logged but not
        # blocked (blanket blocking breaks routine pipelines like `ps | head`).
        segments = self._command_segments(tokens)
        for segment in segments:
            segment_command, words = self._segment_command(segment)
            if not segment_command:
                continue
            if segment_command == "sudo":
                if not allow_sudo:
                    self.blocked_commands.append(command)
                    return False, "Sudo commands not allowed in this context"
                if not words:
                    return False, "Incomplete sudo command"
                segment_command = os.path.basename(words[0]).lower()
            if segment_command not in self.ALLOWED_COMMANDS:
                logger.warning(f"Command not in allowed list: {segment_command}")
                # Don't block, but log for monitoring

        return True, ""

    def sanitize_command_output(self, output: str) -> str:
        """
        Sanitize command output to remove potentially sensitive information

        Args:
            output: Raw command output

        Returns:
            Sanitized output
        """
        if not output:
            return output

        # Remove potential credentials from output
        patterns_to_redact = [
            (r"password[=:]\s*\S+", "password=***"),
            (r"token[=:]\s*\S+", "token=***"),
            (r"key[=:]\s*\S+", "key=***"),
            (r"secret[=:]\s*\S+", "secret=***"),
            (r"api[_-]?key[=:]\s*\S+", "api_key=***"),
            (r"auth[_-]?token[=:]\s*\S+", "auth_token=***"),
        ]

        sanitized = output
        for pattern, replacement in patterns_to_redact:
            sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)

        return sanitized

    def generate_session_token(self) -> str:
        """Generate a secure session token"""
        return secrets.token_urlsafe(32)

    def hash_sensitive_data(self, data: str) -> str:
        """Hash sensitive data for logging purposes"""
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def log_security_event(self, event_type: str, details: str, severity: str = "INFO"):
        """Log security-related events"""
        from datetime import datetime

        event = {"timestamp": str(datetime.now()), "type": event_type, "details": details, "severity": severity}
        self.security_log.append(event)
        logger.log(getattr(logging, severity), f"Security Event: {event_type} - {details}")

    def get_security_summary(self) -> dict:
        """Get summary of security events and blocked operations"""
        return {
            "blocked_commands": len(self.blocked_commands),
            "security_events": len(self.security_log),
            "recent_events": self.security_log[-10:] if self.security_log else [],
        }


# Global security validator instance
security_validator = SecurityValidator()
