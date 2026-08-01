import asyncio
from datetime import datetime

from mcp.types import CallToolResult, TextContent

from ..ssh_manager import get_tunnel_manager
from ._platform import MACOS, WINDOWS, platform_family
from ._results import error_result, failure_detail

# `duration` used to be read and then printed without ever being used, so a
# caller asking for a minute of observation received one instantaneous sample
# labelled "Duration: 60s". It now controls real sampling, within these bounds:
# a long window must not outlast an MCP client's request timeout, and a report
# must not grow without limit.
MAX_SAMPLE_WINDOW_SECONDS = 120
SAMPLE_INTERVAL_SECONDS = 5
MAX_SAMPLES = 4
MAX_METRIC_OUTPUT_CHARS = 4000


async def handle_get_hardware_info(arguments: dict) -> CallToolResult:
    refresh = arguments.get("refresh", False)

    endpoint = await get_tunnel_manager().auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

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
        return error_result("Failed to retrieve hardware information")


def bounded_metric_output(text: str) -> str:
    """Return command output capped to a stated size.

    A single unbounded metric (a busy process table, a long GPU dump) would
    otherwise be pulled into the caller's context in full.
    """
    if len(text) <= MAX_METRIC_OUTPUT_CHARS:
        return text
    kept = text[:MAX_METRIC_OUTPUT_CHARS]
    dropped = len(text) - MAX_METRIC_OUTPUT_CHARS
    return f"{kept}\n[output truncated: {dropped} more characters]"


def sample_plan(window_seconds: int) -> tuple[int, float]:
    """Return how many samples to take over a window, and the gap between them.

    A zero or sub-second window means one instantaneous sample, which is what
    this tool has always done. Longer windows are spread over at most
    `MAX_SAMPLES` rounds so a long observation stays a bounded report.
    """
    if window_seconds <= 0:
        return 1, 0.0
    samples = min(MAX_SAMPLES, max(2, window_seconds // SAMPLE_INTERVAL_SECONDS + 1))
    return samples, window_seconds / (samples - 1)


async def handle_system_monitoring(arguments: dict) -> CallToolResult:
    metrics = arguments.get("metrics", ["all"])

    # Absent means "one sample now", which keeps every existing caller's
    # latency unchanged. A caller that asks for a window gets that window.
    requested_duration = arguments.get("duration")
    if requested_duration is None:
        window_seconds = 0
    elif isinstance(requested_duration, bool) or not isinstance(requested_duration, int):
        return error_result("The duration argument must be an integer number of seconds")
    elif requested_duration < 0:
        return error_result("The duration argument must not be negative")
    else:
        window_seconds = min(requested_duration, MAX_SAMPLE_WINDOW_SECONDS)

    manager = get_tunnel_manager()
    endpoint = await manager.auto_failover()
    if not endpoint:
        return error_result("No available hardware endpoints")

    commands = []
    family = platform_family(endpoint.platform)

    if family == WINDOWS:
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
    elif family == MACOS:
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

    if not commands:
        return error_result("No supported monitoring metrics requested")

    # A small bound avoids exhausting an SSH endpoint's connection limit while
    # eliminating the previous one-command-at-a-time monitoring latency. Each
    # command still produces an independent result, so one failed metric cannot
    # discard the others.
    endpoint_limit = getattr(endpoint, "max_connections", 4)
    endpoint_in_use = getattr(endpoint, "current_connections", 0)
    if not isinstance(endpoint_limit, int) or endpoint_limit <= 0:
        endpoint_limit = 4
    if not isinstance(endpoint_in_use, int) or endpoint_in_use < 0:
        endpoint_in_use = 0
    available_connections = max(1, endpoint_limit - endpoint_in_use)
    semaphore = asyncio.Semaphore(min(4, len(commands), available_connections))

    async def execute_metric(command: str) -> tuple[str, bool]:
        async with semaphore:
            try:
                result = await manager.execute_command(endpoint, command)
            except Exception as exc:
                detail = str(exc).strip() or type(exc).__name__
                return f"Command: {command}\nError: {detail}\n", False
        if result["success"]:
            return f"Command: {command}\n{bounded_metric_output(str(result['stdout']))}\n", True
        detail = failure_detail(result, "Monitoring command failed")
        return f"Command: {command}\nError: {detail}\n", False

    sample_count, interval = sample_plan(window_seconds)
    started = asyncio.get_running_loop().time()
    results: list[str] = []
    all_succeeded = True

    for sample_index in range(sample_count):
        if sample_index:
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, sample_index * interval - elapsed))
        offset = asyncio.get_running_loop().time() - started
        metric_results = await asyncio.gather(*(execute_metric(command) for command in commands))
        all_succeeded = all_succeeded and all(succeeded for _, succeeded in metric_results)
        if sample_count > 1:
            results.append(f"--- Sample {sample_index + 1} of {sample_count} at +{offset:.1f}s ---")
        results.extend(text for text, _ in metric_results)

    observed = asyncio.get_running_loop().time() - started
    result_text = f"System Monitoring Report for {endpoint.hostname}\n"
    if sample_count > 1:
        result_text += f"Samples: {sample_count} over {observed:.1f}s (requested {requested_duration}s)\n"
    else:
        result_text += "Samples: 1 (instantaneous; pass duration to sample over a window)\n"
    if isinstance(requested_duration, int) and requested_duration > MAX_SAMPLE_WINDOW_SECONDS:
        result_text += f"Requested duration capped at {MAX_SAMPLE_WINDOW_SECONDS}s\n"
    result_text += f"Timestamp: {datetime.now().isoformat()}\n\n"
    result_text += "\n".join(results)
    if not all_succeeded:
        return error_result(result_text)
    return CallToolResult(content=[TextContent(type="text", text=result_text)])
