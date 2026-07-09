import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stdio_transport_lists_tools(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rigout.server"],
        cwd=str(tmp_path),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_result = await session.list_tools()

    tool_names = {tool.name for tool in tools_result.tools}
    assert "execute_command" in tool_names
    assert "file_operations" in tool_names
    assert "manage_tunnels" in tool_names
