"""Normal traffic must pass through the proxy unchanged."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from .helpers import SERVERS, TOY, proxied, session


def test_bytes_are_forwarded_unchanged_both_ways(tmp_path):
    record = tmp_path / "record.bin"
    # Unusual but valid formatting: spacing, key order and non-ASCII text must survive.
    reply = '{ "result" : {"ok":true, "text":"café \\u00e9"}, "id":__ID__, "jsonrpc":"2.0" }'
    sent = [
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n',
        b'{ "method" : "notifications/initialized" , "jsonrpc":"2.0"}\n',
        '{"jsonrpc":"2.0","id":"a-2","method":"tools/call","params":{"name":"x","arguments":{"s":"été"}}}\n'.encode(),
        b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}\n',
    ]
    env = {**os.environ, "ECHO_RECORD": str(record), "ECHO_REPLY": reply}
    cmd = proxied([sys.executable, str(SERVERS / "echo_server.py")])
    out = subprocess.run(cmd, input=b"".join(sent), capture_output=True, env=env, timeout=20)

    assert out.returncode == 0, out.stderr.decode()
    assert record.read_bytes() == b"".join(sent)
    expected = [
        reply.replace("__ID__", json.dumps(i)).encode() + b"\n" for i in (1, "a-2", 3)
    ]
    assert out.stdout == b"".join(expected)


async def test_results_through_proxy_match_direct_results():
    calls = [("add", {"a": 2, "b": 3.5}), ("search", {"query": "ACME"}), ("page", {"url": "u"})]

    async def collect(cmd):
        async with session(cmd) as s:
            tools = [t.name for t in (await s.list_tools()).tools]
            results = [(await s.call_tool(n, a)).model_dump() for n, a in calls]
            return tools, results

    assert await collect(proxied(TOY)) == await collect(TOY)
