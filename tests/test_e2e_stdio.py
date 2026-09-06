"""End-to-end: boot the server over stdio as a subprocess and talk to it with a
real MCP client. This exercises mcp.run() / the stdio transport, which the
unit tests don't touch. No network calls are made (only initialize + list_tools).
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {
    "get_electricity_consumption",
    "get_gas_consumption",
    "get_unit_rates",
    "get_standing_charges",
    "list_meter_points",
    "get_agreements",
}


def test_stdio_server_lists_tools():
    async def run():
        env = dict(os.environ)
        env["OCTOPUS_API_KEY"] = "dummy"
        env["OCTOPUS_ACCOUNT_NUMBER"] = "A-TEST123"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "octopus_mcp", "--stdio"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return {t.name for t in result.tools}

    names = asyncio.run(run())
    missing = EXPECTED - names
    assert not missing, f"server did not expose: {missing}"
