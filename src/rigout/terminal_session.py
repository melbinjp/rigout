from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import paramiko

if TYPE_CHECKING:
    from .ssh_manager import TunnelEndpoint


@dataclass
class TerminalSession:
    """Represents an active terminal session"""

    session_id: str
    endpoint: "TunnelEndpoint"
    ssh_client: paramiko.SSHClient
    channel: paramiko.Channel
    created: datetime
    last_activity: datetime
    is_interactive: bool = False
    working_directory: str = "~"
    command_history: list[str] | None = None
    max_idle_time: int = 3600  # 1 hour

    def __post_init__(self):
        if self.command_history is None:
            self.command_history = []

    def is_expired(self) -> bool:
        """Check if session has expired due to inactivity"""
        return (datetime.now() - self.last_activity).seconds > self.max_idle_time

    def add_command(self, command: str):
        """Add command to history with size limit"""
        if self.command_history is None:
            self.command_history = []
        self.command_history.append(command)
        if len(self.command_history) > 100:  # Keep last 100 commands
            self.command_history = self.command_history[-100:]
        self.last_activity = datetime.now()
