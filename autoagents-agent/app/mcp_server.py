# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""MCP server exposing the autoagents tools.

The same functions the root agent uses in-process (app/tools.py) are served here
over the Model Context Protocol, so the Cloud Run gateway, other agents, or an
ADK MCPToolset can reuse them without duplicating logic.

Run locally (stdio):      uv run python -m app.mcp_server
Run as HTTP (for reuse):  uv run python -m app.mcp_server --http   # :8000/mcp

Requires the optional `mcp` dependency:  uv sync --extra mcp
"""
from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from app import tools

mcp = FastMCP("autoagents-tools")

# Register the same callables the agent uses. FastMCP derives the JSON schema
# from each function's type hints and docstring.
for _fn in (
    tools.send_email,
    tools.schedule_task,
    tools.list_tasks,
    tools.cancel_task,
    tools.query_messages,
    tools.get_agent_state,
    tools.set_agent_state,
    tools.current_time,
    tools.search_documents,
    tools.ingest_document,
):
    mcp.tool()(_fn)


if __name__ == "__main__":
    if "--http" in sys.argv:
        # Streamable HTTP transport at http://localhost:8000/mcp
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # stdio
