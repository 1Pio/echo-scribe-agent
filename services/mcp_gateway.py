# services/mcp_gateway.py
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@dataclass(frozen=True)
class GatewaySpec:
    servers: list[str]
    tools_allowlist: list[str]  # Docker syntax: ["duckduckgo:search"] etc.
    transport: str = "stdio"


class DockerMcpGatewayClient:
    """
    Talks to 'docker mcp gateway run' over stdio using the MCP Python SDK.

    Two modes:
      - persistent=True: keep the gateway process + MCP session open (fastest).
      - persistent=False: spawn gateway per call (strict lifecycle, slower).
    """
    def __init__(
        self,
        spec: GatewaySpec,
        *,
        persistent: bool = True,
        call_timeout_s: float = 30.0,
        docker_command: str = "docker",
        extra_gateway_args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ):
        self._spec = spec
        self._persistent = persistent
        self._timeout = call_timeout_s
        self._docker_command = docker_command
        self._extra_gateway_args = extra_gateway_args or []
        self._env = env or {}

        self._lock = asyncio.Lock()
        self._stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None

        # Runtime enforcement too (your own allowlist, independent from Docker’s).
        self._mcp_tool_names_allowlist: set[str] = set()

    def _gateway_args(self) -> list[str]:
        args = [
            "mcp",
            "gateway",
            "run",
            "--transport",
            self._spec.transport,
            "--servers",
            ",".join(self._spec.servers),
        ]
        for t in self._spec.tools_allowlist:
            args += ["--tools", t]
        args += self._extra_gateway_args
        return args

    async def start(self, *, mcp_tool_names_allowlist: Iterable[str]) -> None:
        """
        Start persistent gateway + MCP session and initialize.
        Only needed if persistent=True.
        """
        self._mcp_tool_names_allowlist = set(mcp_tool_names_allowlist)

        if not self._persistent:
            return

        if self._session is not None:
            return

        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=self._docker_command,
            args=self._gateway_args(),
            env=self._env,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Calls an MCP tool by name, with strict allowlisting.
        """
        if self._mcp_tool_names_allowlist and tool_name not in self._mcp_tool_names_allowlist:
            raise PermissionError(f"Tool '{tool_name}' is not allowlisted for this agent.")

        async with self._lock:
            if self._persistent:
                if self._session is None:
                    raise RuntimeError("DockerMcpGatewayClient not started. Call start().")
                return await asyncio.wait_for(
                    self._session.call_tool(tool_name, arguments),
                    timeout=self._timeout,
                )

            # One-shot: spawn gateway + session per call (strict lifecycle)
            params = StdioServerParameters(
                command=self._docker_command,
                args=self._gateway_args(),
                env=self._env,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await asyncio.wait_for(
                        session.call_tool(tool_name, arguments),
                        timeout=self._timeout,
                    )
