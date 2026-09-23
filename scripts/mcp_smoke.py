"""Read-only MCP client verification; credentials are read from the environment."""

from __future__ import annotations

import argparse
import asyncio
import os

import httpx
from mcp.client.streamable_http import streamable_http_client

from mcp import ClientSession


async def verify(url: str, query: str | None, kb_id: str | None) -> None:
    key = os.environ["CAIRN_API_KEY"]
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30) as http,
        streamable_http_client(url, http_client=http) as (reader, writer, _session_id),
        ClientSession(reader, writer) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()
        print(f"Connected: {initialized.serverInfo.name}; protocol={initialized.protocolVersion}")
        print("Tools:", ", ".join(tool.name for tool in tools.tools))
        if query is not None:
            arguments: dict[str, str | int] = {"query": query, "top_k": 5}
            if kb_id is not None:
                arguments["knowledge_base_id"] = kb_id
            result = await session.call_tool("search_knowledge_base", arguments)
            if result.isError:
                error = result.structuredContent or {}
                raise RuntimeError(f"MCP search failed: {error.get('code', 'TOOL_ERROR')}")
            payload = result.structuredContent or {}
            print(f"Search succeeded: {len(payload.get('results', []))} passages")
            print("Request ID:", payload.get("request_id", "unknown"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/mcp")
    parser.add_argument("--query")
    parser.add_argument("--kb-id")
    args = parser.parse_args()
    asyncio.run(verify(args.url, args.query, args.kb_id))
