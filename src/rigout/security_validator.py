#!/usr/bin/env python3
"""
Security Validation Module for Enhanced MCP Hardware Server
Provides comprehensive security checks and validation
"""

import hashlib
import logging
import os
import re
import secrets
import shlex
import stat

logger = logging.getLogger(__name__)


class SecurityValidator:
    """Comprehensive security validation for MCP server operations"""

    # Dangerous command patterns that should be blocked or sanitized
    DANGEROUS_PATTERNS = [
        r"rm\s+-rf\s+/",
        r"dd\s+if=.*of=.*",
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
        r";\s*rm\s+",
        r"&&\s*rm\s+",
        r"\|\s*rm\s+",
        r"nc\s+.*\s+\d+.*<",
        r"netcat\s+.*\s+\d+.*<",
    ]

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
    def _segment_command(segment: list[str]) -> tuple[str, list[str]]:
        """Return the executable and arguments from one shell segment."""
        words = list(segment)
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words.pop(0)
        if not words:
            return "", []
        return os.path.basename(words[0]).lower(), words[1:]

    def _semantic_danger(self, tokens: list[str]) -> str | None:
        """Detect destructive behavior that quote masking alone could hide."""
        raw_devices = re.compile(r"/dev/(?:sd[a-z]|hd[a-z]|nvme\w*|mmcblk\w*|mem\b|kmem|port)$", re.IGNORECASE)
        for index, token in enumerate(tokens[:-1]):
            if token in {">", ">>", "<", "<<"} and raw_devices.fullmatch(tokens[index + 1]):
                return "raw device redirection"

        segments = self._command_segments(tokens)
        for segment in segments:
            executable, args = self._segment_command(segment)
            if executable == "sudo" and args:
                executable = os.path.basename(args[0]).lower()
                args = args[1:]

            if executable == "rm":
                flags = "".join(arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--"))
                targets = [arg for arg in args if not arg.startswith("-")]
                if (
                    "r" in flags
                    and "f" in flags
                    and any(target == "/" or target.startswith("/*") for target in targets)
                ):
                    return "executable rm -rf /"

            if executable in {"bash", "sh"} and len(args) >= 2 and args[0] == "-c":
                nested_safe, nested_error = self.validate_command(args[1], allow_sudo=True)
                if not nested_safe:
                    return nested_error

        return None

    def validate_hostname(self, hostname: str) -> tuple[bool, str]:
        """
        Validate hostname for security and format compliance

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
                return False, f"Command contains dangerous pattern: {pattern}"

        semantic_danger = self._semantic_danger(tokens)
        if semantic_danger:
            self.blocked_commands.append(command)
            return False, f"Command contains dangerous pattern: {semantic_danger}"

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

    def validate_file_path(self, file_path: str, operation: str = "read") -> tuple[bool, str]:
        """
        Validate file path for security risks

        Args:
            file_path: The file path to validate
            operation: The operation type (read, write, execute)

        Returns:
            Tuple of (is_safe, error_message)
        """
        if not file_path or not isinstance(file_path, str):
            return False, "File path must be a non-empty string"

        # Normalize path
        try:
            normalized_path = os.path.normpath(file_path)
        except Exception as e:
            return False, f"Invalid file path: {e}"

        # Check for path traversal attempts
        if ".." in normalized_path:
            return False, "Path traversal attempt detected"

        # Check for access to sensitive system files
        sensitive_paths = [
            "/etc/passwd",
            "/etc/shadow",
            "/etc/sudoers",
            "/root/",
            "/proc/kcore",
            "/dev/mem",
            "/dev/kmem",
            "/sys/firmware/",
            "/boot/",
        ]

        for sensitive_path in sensitive_paths:
            normalized_sensitive = os.path.normpath(sensitive_path)
            if normalized_path.startswith(normalized_sensitive):
                if operation in ["write", "execute"]:
                    return False, f"Write/execute access denied to sensitive path: {sensitive_path}"
                else:
                    logger.warning(f"Read access to sensitive path: {normalized_path}")

        return True, ""

    def validate_ssh_key(self, key_path: str) -> tuple[bool, str]:
        """
        Validate SSH private key file

        Args:
            key_path: Path to the SSH private key

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not key_path or not isinstance(key_path, str):
            return False, "Key path must be a non-empty string"

        # Check if file exists
        if not os.path.exists(key_path):
            return False, f"SSH key file not found: {key_path}"

        # Check file permissions
        try:
            file_stat = os.stat(key_path)
            file_mode = stat.filemode(file_stat.st_mode)

            # SSH keys should have restrictive permissions (600 or 400)
            if file_stat.st_mode & 0o077:  # Check if group/other have any permissions
                return False, f"SSH key has insecure permissions: {file_mode}"

        except Exception as e:
            return False, f"Cannot check key file permissions: {e}"

        # Basic key format validation
        try:
            with open(key_path) as f:
                key_content = f.read()

            # Check for valid key headers
            valid_headers = [
                "-----BEGIN OPENSSH PRIVATE KEY-----",
                "-----BEGIN RSA PRIVATE KEY-----",
                "-----BEGIN EC PRIVATE KEY-----",
                "-----BEGIN PRIVATE KEY-----",
            ]

            if not any(header in key_content for header in valid_headers):
                return False, "Invalid SSH key format"

        except Exception as e:
            return False, f"Cannot read SSH key file: {e}"

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
