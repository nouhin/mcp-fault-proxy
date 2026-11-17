"""Summarise token usage and timing from an LM Studio server log for one batch of runs.

    uv run python demo/lmstudio_usage.py <server log> <start HH:MM:SS> <end HH:MM:SS> <runs>
"""

import json
import re
import sys
from datetime import datetime

path, start, end, runs = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
stamp = re.compile(r"^\[(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\]")
usage_open = re.compile(r'^\s*"usage": \{')
evals = re.compile(r"(prompt eval|        eval) time =\s+([\d.]+) ms /\s+(\d+) tokens")

now = "00:00:00"
calls, prompt, completion, reasoning = 0, 0, 0, 0
gen_ms, gen_tok, pp_ms, pp_tok = 0.0, 0, 0.0, 0
lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
i = 0
while i < len(lines):
    line = lines[i]
    if m := stamp.match(line):
        now = m.group(2)
    inside = start <= now <= end
    if inside and usage_open.match(line):
        block = ["{"]
        i += 1
        depth = 1
        while depth:
            block.append(lines[i])
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
        u = json.loads("\n".join(block).rstrip(","))
        calls += 1
        prompt += u["prompt_tokens"]
        completion += u["completion_tokens"]
        reasoning += (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        continue
    if inside and (m := evals.search(line)):
        if m.group(1) == "prompt eval":
            pp_ms, pp_tok = pp_ms + float(m.group(2)), pp_tok + int(m.group(3))
        else:
            gen_ms, gen_tok = gen_ms + float(m.group(2)), gen_tok + int(m.group(3))
    i += 1

wall_s = (datetime.strptime(end, "%H:%M:%S") - datetime.strptime(start, "%H:%M:%S")).total_seconds()
print(json.dumps({
    "runs": runs,
    "llm_calls": calls,
    "llm_calls_per_run": round(calls / runs, 1),
    "prompt_tokens": prompt,
    "completion_tokens": completion,
    "reasoning_tokens": reasoning,
    "prompt_tokens_per_run": round(prompt / runs),
    "completion_tokens_per_run": round(completion / runs),
    "prompt_tokens_evaluated_after_cache": pp_tok,
    "decode_tokens_per_s_per_request": round(gen_tok / (gen_ms / 1000), 1) if gen_ms else None,
    "batch_wall_s": wall_s,
    "throughput_completion_tokens_per_s": round(completion / wall_s, 1),
}, indent=2))
