"""Real MCP handshake over stdio: spawn ``rcf mcp serve`` and talk to it.

This is the only test that proves the *shipped* server works as an MCP server
(initialize → list_tools → call_tool), rather than just the handler functions
in-process.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from conftest import make_home, toml_path

mcp = pytest.importorskip("mcp")

from mcp import StdioServerParameters  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

pytestmark = pytest.mark.e2e

PROJECT = """\
[projects.demo]
entrypoint = "{{python}} {{script}} --seed {{seed}}"
output_contract = "env"
script = "compute.py"
paths = {{ "laptop" = "{path}" }}
"""


@pytest.fixture
def home(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "compute.py").write_text("print('ok')\n", encoding="utf-8")
    return make_home(
        tmp_path,
        nodes='[nodes.laptop]\nkind = "local"\n',
        projects=PROJECT.format(path=toml_path(project)),
    )


def test_stdio_server_speaks_mcp(home):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rcf.cli.main", "--home", str(home), "mcp", "serve"],
        env={**os.environ, "RCF_HOME": str(home)},
    )

    async def scenario() -> tuple[set[str], dict]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                result = await session.call_tool("compute_list_nodes", {})
                nodes = json.loads(result.content[0].text)
                planned = await session.call_tool(
                    "compute_plan_run", {"project_id": "demo", "params": {"seed": 5}}
                )
                plan = json.loads(planned.content[0].text)
                return names, nodes, plan

    import anyio

    names, nodes, plan = anyio.run(scenario)
    assert "compute_plan_run" in names
    assert "compute_submit_run" in names
    assert nodes["ok"] is True
    assert nodes["data"][0]["id"] == "laptop"
    # The params schema (a str|int|float|bool|None union) must survive the wire.
    assert plan["ok"] is True, plan
    assert plan["data"]["command"]["argv"][-2:] == ["--seed", "5"]
