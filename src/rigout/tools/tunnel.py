from mcp.types import CallToolResult, TextContent

from ..ssh_manager import get_tunnel_manager


async def handle_connect_hardware(arguments: dict) -> CallToolResult:
    preferred_platform = arguments.get("preferred_platform", "any")

    manager = get_tunnel_manager()
    endpoint = None
    if preferred_platform and preferred_platform != "any":
        for candidate in manager.endpoints:
            if preferred_platform.lower() in (candidate.platform or "").lower() and await manager.test_endpoint(
                candidate
            ):
                endpoint = candidate
                manager.active_endpoint = candidate
                break

    if not endpoint:
        endpoint = await manager.auto_failover()

    if not endpoint:
        return CallToolResult(
            content=[
                TextContent(type="text", text="No available hardware endpoints. Please add tunnel endpoints first.")
            ]
        )

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
        hostname = arguments["hostname"]
        username = arguments["username"]
        private_key_path = arguments["private_key_path"]
        platform = arguments.get("platform", "unknown")

        endpoint = get_tunnel_manager().add_endpoint(hostname, username, private_key_path, platform)

        result_text = f"Added tunnel endpoint: {hostname}\n"
        result_text += f"Platform: {platform}\n"
        result_text += f"Username: {username}\n"
        result_text += "Status: Testing connection..."
        return CallToolResult(content=[TextContent(type="text", text=result_text)])

    elif action == "remove":
        hostname = arguments.get("hostname")
        if not hostname:
            return CallToolResult(
                content=[TextContent(type="text", text="The remove action requires a hostname argument")]
            )

        manager = get_tunnel_manager()
        remaining = [endpoint for endpoint in manager.endpoints if endpoint.hostname != hostname]
        removed_count = len(manager.endpoints) - len(remaining)
        if not removed_count:
            return CallToolResult(
                content=[TextContent(type="text", text=f"No tunnel endpoint found with hostname: {hostname}")]
            )

        manager.endpoints = remaining
        if manager.active_endpoint and manager.active_endpoint.hostname == hostname:
            manager.active_endpoint = None
        manager.save_config()
        return CallToolResult(
            content=[TextContent(type="text", text=f"Removed {removed_count} tunnel endpoint(s) for {hostname}")]
        )

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
            return CallToolResult(content=[TextContent(type="text", text="Failover failed: No available endpoints")])
    else:
        return CallToolResult(content=[TextContent(type="text", text=f"Unsupported tunnel action: {action}")])
