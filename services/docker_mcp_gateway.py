# services/docker_mcp_gateway.py
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewaySpec:
    servers: tuple[str, ...]
    tools: tuple[str, ...]
    transport: str = "stdio"
    watch: bool = False
    log_calls: bool = False
    verbose: bool = False


class DockerMCPGateway:
    """
    Lightweight MCP client that spawns `docker mcp gateway run` and talks MCP over stdio.

    Key behavior:
    - Tool allowlisting via `--servers` + `--tools`
    - Keeps the gateway warm for `idle_ttl_s` seconds for performance
    - Still lets Docker MCP Toolkit spawn/stop tool containers on-demand (do NOT set --long-lived)
    """

    def __init__(self, spec: GatewaySpec, *, idle_ttl_s: float = 30.0) -> None:
        self._spec = spec
        self._idle_ttl_s = float(idle_ttl_s)

        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._last_used = 0.0

    def _docker_args(self) -> list[str]:
        # Docker docs: `docker mcp gateway run` supports --servers, --tools, --transport, --watch, etc. :contentReference[oaicite:2]{index=2}
        args = ["mcp", "gateway", "run"]

        if self._spec.servers:
            args.append(f"--servers={','.join(self._spec.servers)}")
        if self._spec.tools:
            args.append(f"--tools={','.join(self._spec.tools)}")

        args.append(f"--transport={self._spec.transport}")
        args.append(f"--watch={'true' if self._spec.watch else 'false'}")
        args.append(f"--log-calls={'true' if self._spec.log_calls else 'false'}")

        if self._spec.verbose:
            args.append("--verbose")

        return args

    def _merged_env(self) -> dict[str, str]:
        # IMPORTANT on Windows: subprocess env must include PATH + profile vars.
        env = os.environ.copy()
        for k in (
            "PATH",
            "USERPROFILE",
            "HOME",
            "APPDATA",
            "LOCALAPPDATA",
            "ProgramFiles",
            "ProgramFiles(x86)",
        ):
            if k not in env:
                env[k] = ""
        return env

    async def start(self) -> None:
        async with self._lock:
            if self._session is not None:
                return

            stack = AsyncExitStack()
            try:
                params = StdioServerParameters(
                    command="docker",
                    args=self._docker_args(),
                    env=self._merged_env(),
                )

                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))

                await session.initialize()

                self._stack = stack
                self._session = session
                self._last_used = time.monotonic()

            except Exception:
                await stack.aclose()
                raise

    async def stop(self) -> None:
        async with self._lock:
            if self._stack is not None:
                try:
                    await self._stack.aclose()
                finally:
                    self._stack = None
                    self._session = None

    async def _stop_if_idle(self) -> None:
        if self._idle_ttl_s <= 0:
            return
        if self._session is None:
            return
        if (time.monotonic() - self._last_used) > self._idle_ttl_s:
            await self.stop()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        await self.start()
        assert self._session is not None

        self._last_used = time.monotonic()

        try:
            result = await self._session.call_tool(name, arguments)
        except McpError as e:
            # If the docker gateway process exited early, you commonly see "Connection closed".
            if "Connection closed" in str(e):
                log.warning("MCP gateway connection closed; restarting once")
                await self.stop()
                await self.start()
                assert self._session is not None
                result = await self._session.call_tool(name, arguments)
            else:
                raise
        finally:
            self._last_used = time.monotonic()
            if self._idle_ttl_s == 0:
                await self.stop()
            else:
                await self._stop_if_idle()

        # MCP returns content blocks; normalize to plain text
        chunks: list[str] = []
        for block in getattr(result, "content", []) or []:
            txt = getattr(block, "text", None)
            if txt:
                chunks.append(txt)
        return "\n".join(chunks).strip()
