import contextlib
import logging
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from ..ssh_manager import get_tunnel_manager
from ._platform import platform_family, platform_tokens
from ._results import error_result

logger = logging.getLogger(__name__)


def platform_matches(preferred: object, candidate_platform: object) -> bool:
    """Decide whether an endpoint satisfies a preferred_platform request.

    `windows`, `linux` and `macos` are matched by family, so an endpoint
    labelled "Ubuntu 22.04" satisfies a request for linux and a Mac ("darwin")
    never satisfies a request for windows. Anything else, such as "docker",
    falls back to a token match on the label.
    """
    wanted = str(preferred).strip().lower()
    if wanted in {"windows", "linux", "macos", "darwin", "mac"}:
        return platform_family(candidate_platform) == platform_family(wanted)
    return wanted in platform_tokens(candidate_platform)


def close_endpoint_resources(manager: object, hostname: str) -> tuple[int, int]:
    """Close every live SSH client and terminal session bound to a hostname.

    Dropping an endpoint from the configuration is not enough: pooled SSH
    clients stay open and authenticated, so `execute_in_terminal` on an
    existing session still reaches a host the user has just removed.

    This walks the manager's connection pool directly because TunnelManager
    exposes no per-endpoint close; the natural home for it is a
    `close_endpoint_connections` method on TunnelManager in ssh_manager.py.
    """
    closed_clients = 0
    pool = getattr(manager, "_connection_pool", None)
    if isinstance(pool, dict):
        for pool_key in [key for key in list(pool) if str(key).rsplit(":", 1)[0] == hostname]:
            for ssh_client in pool.pop(pool_key, []) or []:
                with contextlib.suppress(Exception):
                    ssh_client.close()
                closed_clients += 1

    closed_sessions = 0
    sessions = getattr(manager, "terminal_sessions", None)
    if isinstance(sessions, dict):
        for session_id, session in list(sessions.items()):
            endpoint = getattr(session, "endpoint", None)
            if endpoint is not None and getattr(endpoint, "hostname", None) == hostname:
                with contextlib.suppress(Exception):
                    manager.close_terminal_session(session_id)  # type: ignore[attr-defined]
                closed_sessions += 1
    return closed_clients, closed_sessions


async def handle_connect_hardware(arguments: dict) -> CallToolResult:
    preferred_platform = arguments.get("preferred_platform", "any")

    manager = get_tunnel_manager()
    endpoint = None
    if preferred_platform and preferred_platform != "any":
        for candidate in manager.endpoints:
            if platform_matches(preferred_platform, candidate.platform) and await manager.test_endpoint(candidate):
                endpoint = candidate
                manager.active_endpoint = candidate
                break

    if not endpoint:
        endpoint = await manager.auto_failover()

    if not endpoint:
        return error_result("No available hardware endpoints. Please add tunnel endpoints first.")

    hardware_info = await get_tunnel_manager().get_hardware_info(endpoint)

    result_text = f"Connected to hardware: {endpoint.hostname}\n\n"
    result_text += f"Platform: {endpoint.platform}\n"
    result_text += f"Response Time: {endpoint.response_time:.2f}s\n"

    if hardware_info:
        result_text += f"Hardware: {hardware_info.cpu_count} CPUs, {hardware_info.memory_gb}GB RAM\n"
        gpu_text = ", ".join(hardware_info.gpu_info) if hardware_info.gpu_info else "None detected"
        result_text += f"GPU: {gpu_text}\n\n"

    result_text += "Full hardware access available! You can now execute commands, install software, access files, and use all system resources."
    return CallToolResult(content=[TextContent(type="text", text=result_text)])


async def handle_manage_tunnels(arguments: dict) -> CallToolResult:
    action = arguments["action"]

    if action == "add":
        # Every one of these used to be arguments["..."], so a missing argument
        # surfaced as "Error executing tool 'manage_tunnels': 'hostname'".
        missing = [name for name in ("hostname", "username", "private_key_path") if not arguments.get(name)]
        if missing:
            return error_result(f"The add action requires: {', '.join(missing)}")

        hostname = str(arguments["hostname"])
        username = str(arguments["username"])
        private_key_path = str(arguments["private_key_path"])
        platform = arguments.get("platform", "unknown")

        manager = get_tunnel_manager()
        existing = next((item for item in manager.endpoints if item.hostname == hostname), None)
        if existing is not None:
            return error_result(
                f"A tunnel endpoint for {hostname} is already configured (user {existing.username}). "
                "Remove it first if you want to replace it."
            )

        expanded_key_path = str(Path(private_key_path).expanduser())
        if not Path(expanded_key_path).is_file():
            return error_result(
                f"Private key not found: {private_key_path}. "
                "The endpoint was not added; correct the path and try again."
            )

        endpoint = manager.add_endpoint(hostname, username, expanded_key_path, platform)

        # The old code claimed "Status: Testing connection..." and then never
        # tested anything, so a bad endpoint looked like a good one until it
        # failed somewhere far away from here.
        try:
            reachable = await manager.test_endpoint(endpoint)
        except Exception as exc:
            logger.warning("Connection test failed for %s: %s", hostname, exc)
            reachable = False
        manager.save_config()

        result_text = f"Added tunnel endpoint: {hostname}\n"
        result_text += f"Platform: {platform}\n"
        result_text += f"Username: {username}\n"
        result_text += f"Private key: {expanded_key_path}\n"
        if reachable:
            result_text += "Status: connection test PASSED"
            return CallToolResult(content=[TextContent(type="text", text=result_text)])
        result_text += (
            "Status: connection test FAILED. The endpoint stays configured so you can retry, "
            "but it is not usable yet - check the host, the username and the key."
        )
        return error_result(result_text)

    elif action == "remove":
        target_hostname = str(arguments.get("hostname") or "")
        if not target_hostname:
            return error_result("The remove action requires a hostname argument")

        manager = get_tunnel_manager()
        remaining = [endpoint for endpoint in manager.endpoints if endpoint.hostname != target_hostname]
        removed_count = len(manager.endpoints) - len(remaining)
        if not removed_count:
            return error_result(f"No tunnel endpoint found with hostname: {target_hostname}")

        manager.endpoints = remaining
        if manager.active_endpoint and manager.active_endpoint.hostname == target_hostname:
            manager.active_endpoint = None
        closed_clients, closed_sessions = close_endpoint_resources(manager, target_hostname)
        manager.save_config()
        result_text = f"Removed {removed_count} tunnel endpoint(s) for {target_hostname}\n"
        result_text += f"Closed pooled SSH connections: {closed_clients}\n"
        result_text += f"Closed terminal sessions: {closed_sessions}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])

    elif action == "list":
        if not get_tunnel_manager().endpoints:
            return CallToolResult(content=[TextContent(type="text", text="No tunnel endpoints configured")])

        result_text = "Configured Tunnel Endpoints:\n\n"
        for i, endpoint in enumerate(get_tunnel_manager().endpoints, 1):
            status_symbol = {"active": "[ACTIVE]", "failed": "[FAILED]", "unknown": "[UNKNOWN]"}.get(
                endpoint.status, "[UNKNOWN]"
            )
            result_text += f"{i}. {status_symbol} {endpoint.hostname}\n"
            result_text += f"   Platform: {endpoint.platform}\n"
            result_text += f"   Status: {endpoint.status}\n"
            result_text += f"   Purpose: {endpoint.purpose}\n"
            if endpoint.response_time:
                result_text += f"   Response Time: {endpoint.response_time:.2f}s\n"
            result_text += "\n"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])

    elif action == "test":
        results = []
        for endpoint in get_tunnel_manager().endpoints:
            success = await get_tunnel_manager().test_endpoint(endpoint)
            results.append(f"{endpoint.hostname}: {'PASS' if success else 'FAIL'}")

        get_tunnel_manager().save_config()

        result_text = "Tunnel Test Results:\n\n"
        result_text += "\n".join(results)
        if any(result.endswith(": FAIL") for result in results):
            return error_result(result_text)
        return CallToolResult(content=[TextContent(type="text", text=result_text)])

    elif action == "failover":
        new_endpoint = await get_tunnel_manager().find_best_endpoint()

        if new_endpoint:
            get_tunnel_manager().active_endpoint = new_endpoint
            get_tunnel_manager().save_config()

            result_text = "Failover successful\n"
            result_text += f"New active endpoint: {new_endpoint.hostname}\n"
            result_text += f"Response time: {new_endpoint.response_time:.2f}s"
            return CallToolResult(content=[TextContent(type="text", text=result_text)])
        else:
            return error_result("Failover failed: No available endpoints")
    else:
        return error_result(f"Unsupported tunnel action: {action}")
