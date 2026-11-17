"""A fake stdio server for byte-level tests.

It appends every line it receives to the file named in ECHO_RECORD and answers
each request (a line with an "id" and a "method") with a fixed result line
taken from ECHO_REPLY, with the request id spliced in.
"""

import json
import os
import sys

record = open(os.environ["ECHO_RECORD"], "ab", buffering=0)
reply = os.environ.get("ECHO_REPLY", '{"jsonrpc":"2.0","id":__ID__,"result":{"ok" : true}}')

for line in sys.stdin.buffer:
    record.write(line)
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if isinstance(msg, dict) and "id" in msg and "method" in msg:
        out = reply.replace("__ID__", json.dumps(msg["id"]))
        sys.stdout.buffer.write(out.encode() + b"\n")
        sys.stdout.buffer.flush()
