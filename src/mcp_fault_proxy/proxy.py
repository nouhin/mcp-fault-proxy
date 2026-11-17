"""Stdio relay between an MCP client and an MCP server.

The client starts the proxy as if it were the server. The proxy starts the real
server as a child process and relays newline-delimited JSON-RPC messages in both
directions. A message that no fault applies to is forwarded as the exact bytes
that were received, so normal traffic is unchanged.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable

# Tool results such as fetched pages can be large; asyncio's default line limit is 64 KiB.
LINE_LIMIT = 64 * 1024 * 1024

Write = Callable[[bytes], Awaitable[None]]


class Relay:
    """Relays lines between the client side and a server subprocess.

    Subclasses override ``on_client_line`` and ``on_server_line`` to change
    traffic. The defaults forward every line unchanged.
    """

    def __init__(self, server_cmd: list[str]) -> None:
        if not server_cmd:
            raise ValueError("server command is empty")
        self.server_cmd = server_cmd
        self._proc: asyncio.subprocess.Process | None = None
        self._client_lock = asyncio.Lock()
        self._server_lock = asyncio.Lock()
        self._client_writer: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # -- hooks -------------------------------------------------------------

    async def on_client_line(self, line: bytes) -> None:
        """Handle one line from the client. Default: forward to the server."""
        await self.send_to_server(line)

    async def on_server_line(self, line: bytes) -> None:
        """Handle one line from the server. Default: forward to the client."""
        await self.send_to_client(line)

    # -- sending -----------------------------------------------------------

    async def send_to_server(self, line: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        async with self._server_lock:
            if self._proc.stdin.is_closing():
                return
            self._proc.stdin.write(_ensure_newline(line))
            try:
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def send_to_client(self, line: bytes) -> None:
        assert self._client_writer is not None
        async with self._client_lock:
            self._client_writer.write(_ensure_newline(line))
            try:
                await self._client_writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def spawn(self, coro: Awaitable[None]) -> None:
        """Run a coroutine in the background, so one slow message never blocks others."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- main loop ---------------------------------------------------------

    async def run(self) -> int:
        loop = asyncio.get_running_loop()

        client_reader = asyncio.StreamReader(limit=LINE_LIMIT)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(client_reader), sys.stdin
        )
        # Duplicate stdout so closing the transport does not close fd 1 under Python.
        out = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, out
        )
        self._client_writer = asyncio.StreamWriter(transport, protocol, None, loop)

        self._proc = await asyncio.create_subprocess_exec(
            *self.server_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # the server's stderr goes straight to ours
            limit=LINE_LIMIT,
        )
        assert self._proc.stdout is not None

        upstream = asyncio.create_task(self._pump(client_reader, self.on_client_line))
        downstream = asyncio.create_task(self._pump(self._proc.stdout, self.on_server_line))

        done, _ = await asyncio.wait({upstream, downstream}, return_when=asyncio.FIRST_COMPLETED)
        if upstream in done:
            # The client closed its side: let in-flight work finish, then close the server.
            await self._drain_tasks()
            if self._proc.stdin is not None and not self._proc.stdin.is_closing():
                self._proc.stdin.close()
            try:
                await asyncio.wait_for(downstream, timeout=5)
            except TimeoutError:
                downstream.cancel()
        else:
            upstream.cancel()

        await self._drain_tasks(cancel=True)
        try:
            code = await asyncio.wait_for(self._proc.wait(), timeout=5)
        except TimeoutError:
            self._proc.kill()
            code = await self._proc.wait()
        return code

    async def _pump(
        self, reader: asyncio.StreamReader, handle: Callable[[bytes], Awaitable[None]]
    ) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            if not line.strip():
                continue
            await handle(line)

    async def _drain_tasks(self, cancel: bool = False) -> None:
        if not self._tasks:
            return
        if cancel:
            for task in self._tasks:
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _ensure_newline(line: bytes) -> bytes:
    return line if line.endswith(b"\n") else line + b"\n"
