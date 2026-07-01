from datetime import datetime

from mcp.types import CallToolResult, TextContent

from ..ssh_manager import get_tunnel_manager


async def handle_get_hardware_info(arguments: dict) -> CallToolResult:
    refresh = arguments.get("refresh", False)

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return CallToolResult(content=[TextContent(type="text", text="No available hardware endpoints")])

    if refresh or endpoint.hostname not in get_tunnel_manager().hardware_cache:
        hardware_info = await get_tunnel_manager().get_hardware_info(endpoint)
    else:
        hardware_info = get_tunnel_manager().hardware_cache[endpoint.hostname]

    if hardware_info:
        result_text = f"Hardware Information for {endpoint.hostname}\n\n"
        result_text += f"Platform: {hardware_info.platform} ({hardware_info.architecture})\n"
        result_text += f"CPUs: {hardware_info.cpu_count}\n"
        result_text += f"Memory: {hardware_info.memory_gb} GB\n"
        result_text += f"Disk Space: {hardware_info.disk_space_gb} GB\n"
        result_text += f"GPU: {', '.join(hardware_info.gpu_info)}\n"
        result_text += f"Last Updated: {hardware_info.last_updated.isoformat()}"
        return CallToolResult(content=[TextContent(type="text", text=result_text)])
    else:
        return CallToolResult(content=[TextContent(type="text", text="Failed to retrieve hardware information")])


async def handle_system_monitoring(arguments: dict) -> CallToolResult:
    metrics = arguments.get("metrics", ["all"])
    duration = arguments.get("duration", 10)

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return CallToolResult(content=[TextContent(type="text", text="No available hardware endpoints")])

    commands = []
    endpoint_platform = (endpoint.platform or "").lower()
    is_windows = "win" in endpoint_platform
    is_macos = "darwin" in endpoint_platform or "mac" in endpoint_platform

    if is_windows:
        if "all" in metrics or "cpu" in metrics:
            commands.append(
                'powershell -NoProfile -Command "Get-CimInstance Win32_Processor | Select-Object -First 1 LoadPercentage,NumberOfLogicalProcessors | ConvertTo-Json -Compress"'
            )
        if "all" in metrics or "memory" in metrics:
            commands.append(
                'powershell -NoProfile -Command "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json -Compress"'
            )
        if "all" in metrics or "disk" in metrics:
            commands.append(
                'powershell -NoProfile -Command "Get-PSDrive -PSProvider FileSystem | Select-Object Name,Used,Free | ConvertTo-Json -Compress"'
            )
        if "all" in metrics or "network" in metrics:
            commands.append(
                'powershell -NoProfile -Command "Get-NetTCPConnection -State Listen | Select-Object -First 10 LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress"'
            )
        if "all" in metrics or "processes" in metrics:
            commands.append(
                'powershell -NoProfile -Command "Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 ProcessName,Id,CPU,WorkingSet | ConvertTo-Json -Compress"'
            )
        if "all" in metrics or "gpu" in metrics:
            commands.append(
                'nvidia-smi 2>NUL || powershell -NoProfile -Command "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"'
            )
    elif is_macos:
        if "all" in metrics or "cpu" in metrics:
            commands.append("top -l 1 | grep 'CPU usage' || sysctl -n hw.ncpu")
        if "all" in metrics or "memory" in metrics:
            commands.append("vm_stat")
        if "all" in metrics or "disk" in metrics:
            commands.append("df -h")
        if "all" in metrics or "network" in metrics:
            commands.append("netstat -an | head -20")
        if "all" in metrics or "processes" in metrics:
            commands.append("ps aux -r | head -10")
        if "all" in metrics or "gpu" in metrics:
            commands.append("system_profiler SPDisplaysDataType | head -40")
    else:
        if "all" in metrics or "cpu" in metrics:
            commands.append("top -bn1 | grep 'Cpu(s)' | head -1")
        if "all" in metrics or "memory" in metrics:
            commands.append("free -h")
        if "all" in metrics or "disk" in metrics:
            commands.append("df -h")
        if "all" in metrics or "network" in metrics:
            commands.append("ss -tuln | head -10")
        if "all" in metrics or "processes" in metrics:
            commands.append("ps aux --sort=-%cpu | head -10")
        if "all" in metrics or "gpu" in metrics:
            commands.append("nvidia-smi 2>/dev/null || echo 'No NVIDIA GPU detected'")

    results = []
    for cmd in commands:
        result = await get_tunnel_manager().execute_command(endpoint, cmd)
        if result["success"]:
            results.append(f"Command: {cmd}\n{result['stdout']}\n")
        else:
            results.append(f"Command: {cmd}\nError: {result.get('error', 'Failed')}\n")

    result_text = f"System Monitoring Report for {endpoint.hostname}\n"
    result_text += f"Duration: {duration}s\n"
    result_text += f"Timestamp: {datetime.now().isoformat()}\n\n"
    result_text += "\n".join(results)
    return CallToolResult(content=[TextContent(type="text", text=result_text)])
