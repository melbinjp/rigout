#!/usr/bin/env python3
"""
MCP Workspace Manager
Provides repository switching and workspace management for agents.
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from mcp.types import TextContent, Tool


class WorkspaceManager:
    """Manages multiple repositories and workspace switching"""

    def __init__(self, base_workspace: str = "/tmp/rigout-workspace"):
        self.base_workspace = Path(base_workspace)
        self.base_workspace.mkdir(parents=True, exist_ok=True)
        self.current_repo = None
        self.repos = {}
        self.state_file = self.base_workspace / ".workspace_state.json"
        self._load_state()

    def _load_state(self):
        """Load workspace state from file"""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    state = json.load(f)
                    self.current_repo = state.get("current_repo")
                    self.repos = state.get("repos", {})
            except Exception:
                pass

    def _save_state(self):
        """Save workspace state to file"""
        state = {"current_repo": self.current_repo, "repos": self.repos, "last_updated": datetime.now().isoformat()}
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def list_repos(self) -> list[dict]:
        """List all repositories in workspace"""
        repos = []

        # Scan workspace for git repos
        for item in self.base_workspace.iterdir():
            if item.is_dir() and (item / ".git").exists():
                repo_info = {
                    "name": item.name,
                    "path": str(item),
                    "is_current": str(item) == self.current_repo,
                    "last_accessed": self.repos.get(str(item), {}).get("last_accessed"),
                }

                # Get git info
                try:
                    result = subprocess.run(
                        ["git", "-C", str(item), "remote", "get-url", "origin"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        repo_info["remote_url"] = result.stdout.strip()
                except Exception:
                    pass

                repos.append(repo_info)

        return repos

    def switch_repo(self, repo_path: str) -> dict:
        """Switch to a different repository"""
        repo_path = Path(repo_path)

        # Handle relative paths
        if not repo_path.is_absolute():
            repo_path = self.base_workspace / repo_path

        # Validate repo exists
        if not repo_path.exists():
            return {"success": False, "error": f"Repository not found: {repo_path}"}

        if not repo_path.is_dir():
            return {"success": False, "error": f"Not a directory: {repo_path}"}

        # Update current repo
        self.current_repo = str(repo_path)

        # Update repo metadata
        if str(repo_path) not in self.repos:
            self.repos[str(repo_path)] = {}

        self.repos[str(repo_path)]["last_accessed"] = datetime.now().isoformat()
        self._save_state()

        # Change directory
        try:
            os.chdir(repo_path)
        except Exception as e:
            return {"success": False, "error": f"Failed to change directory: {e}"}

        return {"success": True, "current_repo": str(repo_path), "cwd": os.getcwd()}

    def clone_repo(self, repo_url: str, destination: str | None = None) -> dict:
        """Clone a repository to workspace"""
        # Determine destination
        if destination:
            dest_path = Path(destination)
            if not dest_path.is_absolute():
                dest_path = self.base_workspace / dest_path
        else:
            # Extract repo name from URL
            repo_name = repo_url.rstrip("/").split("/")[-1]
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]
            dest_path = self.base_workspace / repo_name

        # Check if already exists
        if dest_path.exists():
            return {"success": False, "error": f"Destination already exists: {dest_path}"}

        # Clone repository
        try:
            result = subprocess.run(
                ["git", "clone", repo_url, str(dest_path)], capture_output=True, text=True, timeout=300
            )

            if result.returncode != 0:
                return {"success": False, "error": f"Git clone failed: {result.stderr}"}

            # Add to repos
            self.repos[str(dest_path)] = {"cloned_at": datetime.now().isoformat(), "remote_url": repo_url}
            self._save_state()

            return {"success": True, "path": str(dest_path), "message": f"Successfully cloned {repo_url}"}

        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Clone operation timed out"}
        except Exception as e:
            return {"success": False, "error": f"Clone failed: {e}"}

    def get_current_repo(self) -> dict:
        """Get current repository information"""
        if not self.current_repo:
            return {"current_repo": None, "cwd": os.getcwd()}

        repo_path = Path(self.current_repo)
        info = {
            "current_repo": str(repo_path),
            "name": repo_path.name,
            "cwd": os.getcwd(),
            "exists": repo_path.exists(),
        }

        # Get git info
        if repo_path.exists() and (repo_path / ".git").exists():
            try:
                # Get current branch
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "branch", "--show-current"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    info["branch"] = result.stdout.strip()

                # Get remote URL
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    info["remote_url"] = result.stdout.strip()

                # Get status
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "status", "--porcelain"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    info["has_changes"] = bool(result.stdout.strip())
            except Exception:
                pass

        return info

    def remove_repo(self, repo_path: str, force: bool = False) -> dict:
        """Remove a repository from workspace"""
        repo_path = Path(repo_path)

        if not repo_path.is_absolute():
            repo_path = self.base_workspace / repo_path

        if not repo_path.exists():
            return {"success": False, "error": f"Repository not found: {repo_path}"}

        # Check if it's the current repo
        if str(repo_path) == self.current_repo:
            if not force:
                return {
                    "success": False,
                    "error": "Cannot remove current repository. Switch to another repo first or use force=True",
                }
            self.current_repo = None

        # Remove from tracking
        if str(repo_path) in self.repos:
            del self.repos[str(repo_path)]
            self._save_state()

        return {
            "success": True,
            "message": f"Repository removed from tracking: {repo_path}",
            "note": "Files not deleted. Use rm -rf to delete files.",
        }

    def get_workspace_info(self) -> dict:
        """Get complete workspace information"""
        return {
            "base_workspace": str(self.base_workspace),
            "current_repo": self.current_repo,
            "total_repos": len(self.list_repos()),
            "repos": self.list_repos(),
            "cwd": os.getcwd(),
        }


# MCP Server Integration
def register_workspace_tools(server):
    """Register workspace management tools with MCP server"""

    workspace_manager = WorkspaceManager()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available workspace management tools"""
        return [
            Tool(
                name="list_repos",
                description="List all repositories in workspace",
                inputSchema={"type": "object", "properties": {}, "required": []},
            ),
            Tool(
                name="switch_repo",
                description="Switch to a different repository",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_path": {
                            "type": "string",
                            "description": "Path to repository (absolute or relative to workspace)",
                        }
                    },
                    "required": ["repo_path"],
                },
            ),
            Tool(
                name="clone_repo",
                description="Clone a repository to workspace",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_url": {"type": "string", "description": "Git repository URL to clone"},
                        "destination": {
                            "type": "string",
                            "description": "Optional destination path (relative to workspace)",
                        },
                    },
                    "required": ["repo_url"],
                },
            ),
            Tool(
                name="get_current_repo",
                description="Get information about current repository",
                inputSchema={"type": "object", "properties": {}, "required": []},
            ),
            Tool(
                name="get_workspace_info",
                description="Get complete workspace information",
                inputSchema={"type": "object", "properties": {}, "required": []},
            ),
            Tool(
                name="remove_repo",
                description="Remove a repository from workspace tracking",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "repo_path": {"type": "string", "description": "Path to repository to remove"},
                        "force": {"type": "boolean", "description": "Force removal even if it's the current repo"},
                    },
                    "required": ["repo_path"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Handle workspace management tool calls"""

        try:
            if name == "list_repos":
                result = workspace_manager.list_repos()
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "switch_repo":
                repo_path = arguments.get("repo_path")
                result = workspace_manager.switch_repo(repo_path)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "clone_repo":
                repo_url = arguments.get("repo_url")
                destination = arguments.get("destination")
                result = workspace_manager.clone_repo(repo_url, destination)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "get_current_repo":
                result = workspace_manager.get_current_repo()
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "get_workspace_info":
                result = workspace_manager.get_workspace_info()
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            elif name == "remove_repo":
                repo_path = arguments.get("repo_path")
                force = arguments.get("force", False)
                result = workspace_manager.remove_repo(repo_path, force)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            else:
                return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


if __name__ == "__main__":
    # Standalone testing
    manager = WorkspaceManager()

    print("Workspace Manager Test")
    print("=" * 50)

    # List repos
    print("\nRepositories:")
    repos = manager.list_repos()
    for repo in repos:
        print(f"  - {repo['name']}: {repo['path']}")

    # Get current repo
    print("\nCurrent Repository:")
    current = manager.get_current_repo()
    print(json.dumps(current, indent=2))

    # Get workspace info
    print("\nWorkspace Info:")
    info = manager.get_workspace_info()
    print(json.dumps(info, indent=2))
