# mcp-fault-proxy

A minimal fault-injecting proxy for MCP (Model Context Protocol) servers that use the stdio
transport. It sits between an MCP client and an MCP server, forwards normal traffic unchanged and
injects configured faults into `tools/call` traffic: added latency, error responses, lost calls,
and results that look successful but are wrong.

Faults are labelled with short IDs: **L** for a tool call that fails visibly (L1–L5) and **S** for a
tool result that comes back successful but wrong (S1–S8).

```
MCP client ──stdio──> mcp-fault-proxy ──stdio──> MCP server
           <──────────  (rules, seed)  <──────────
                             │
                             └──> fault log (JSON Lines)
```

## Running it

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python demo/run_demo.py      # every scenario; rewrites demo/demo-run.log
uv run pytest                       # end-to-end checks
```

To put any stdio MCP server behind the proxy, start the proxy in place of the server:

```sh
uv run mcp-fault-proxy -c faults.yaml -- python my_server.py
```

| Option | Meaning |
|---|---|
| `-c, --config FILE` | YAML fault configuration. Without one the proxy only forwards. |
| `--seed N` | Override the configuration's seed. |
| `--log FILE` | Fault log (JSON Lines). Overrides `log:` in the configuration. |
| `-q, --quiet` | Do not echo injected faults to stderr. |

In a client configuration (Claude Desktop, an agent framework, etc.), set the server's
`command` to `mcp-fault-proxy` and its `args` to `["-c", "faults.yaml", "--", "python", "my_server.py"]`.

## Configuration

```yaml
seed: 7                      # fixes which calls are hit and how
log: logs/faults.jsonl
rules:
  - id: one-figure-altered   # names the rule in the log
    tag: S5                  # optional label, here the fault ID
    tool: fetch_page         # a tool name, or "*" for any tool
    fault: alter_number
    rate: 0.3                # probability for each matching call ...
    nth: [2]                 # ... and/or these calls always fire (1-based, per tool)
    max_count: 1             # optional cap on injections by this rule
    field: text              # fault parameters
```

| `fault` | Parameters (default) | What the client sees | ID |
|---|---|---|---|
| `latency` | `delay_ms`, `jitter_ms` (0) | The correct answer, late. Above the client's timeout it becomes a timeout. | L1 |
| `error` | `mode` (`jsonrpc` / `tool`), `code` (-32000), `message` | A JSON-RPC error, or a tool result with `isError: true`. The server never sees the call. | L2, L5 |
| `drop` | none | No answer at all. The server never sees the call. | L4 |
| `rewrite_args` | `argument`, `pattern`, `replacement` | The server answers a different question (e.g. last year's report for this year's URL). | S4 |
| `alter_number` | `field`, `pattern`, `min_change` (0.05), `max_change` (0.15) | One figure changed by ±5–15 %, with its format kept. | S5, S7 |
| `reformat_numbers` | `field`, `pattern` | `4,812.3` becomes `4.812,3`. | S8 |
| `truncate` | `field`, `keep` (0.3) | Text cut off after a fraction of its length. | S2 |
| `replace_text` | `field`, `text` | Text replaced, e.g. a login wall served with status 200. | S3 |
| `empty_result` | none | A successful result with no hits. | S1 |
| `inject_item` | `item` | An extra item ranked first in a list result. | S6 |

`field` limits a result fault to values under keys of that name, e.g. a page's `text` but not its
`url` or `status`. Without it, every string and number in the result is fair game.

## Design choices

- **A byte-level stdio relay, not an SDK client plus an SDK server.** The client starts the proxy as
  if it were the server, and the proxy starts the real server as a child process. Messages are
  newline-delimited JSON-RPC, and any line no fault applies to is forwarded as the exact bytes
  received. The MCP handshake, capability negotiation and notifications pass through untouched,
  so normal traffic is unchanged by construction. Only a faulted message is re-serialised.
- **Faults act only on `tools/call`.** This is where every tool fault of an agent lives.
  `initialize`, `tools/list` and notifications are never touched.
- **Reproducible without global randomness.** Whether a rule fires is a SHA-256 hash of
  `(seed, rule id, tool, n)`, where `n` is the call's position among calls to that tool. A fault's
  own randomness (jitter, which figure to change, by how much) uses a generator seeded from the same
  inputs. Decisions depend neither on timing nor on traffic to other tools, so adding a call to
  `calculator` does not move the faults on `fetch_page`. The same configuration and the same
  sequence of calls give the same faults, as the rerun of `14-mixed-random` in the demo log shows.
- **One fault per call.** Rules are tried in order and the first one that fires wins, which keeps
  every outcome attributable to a single fault.
- **Faults never block other traffic.** A delayed request is sent from a background task, so other
  calls on the same session carry on.
- **Wrong results stay consistent.** Result faults rewrite both the text content blocks and
  `structuredContent`, re-parsing text blocks that hold JSON. The client sees one wrong value, not
  two copies that disagree, which would give the fault away.
- **Every injected fault is logged** as one JSON line: time, seed, rule, tag, fault, tool, call
  index, request id, arguments, and details (delay, error message, or the changed text before and
  after). A short line also goes to stderr unless `-q` is given.

## Demo

`demo/` holds two toy servers built with the official MCP Python SDK. They provide the tools a report-writing
agent needs:

- `web` (`web_search`, `fetch_page`) serves a small fixed web of annual reports for three fictional
  companies, plus one unofficial aggregator page.
- `math` (`calculator`) is a safe arithmetic evaluator.

`demo/report_client.py` is a scripted client with no LLM. It runs an example task
(FY2025 revenue and year-on-year growth for three companies) with both servers behind the proxy,
then grades its report against a hand-written answer key. The grades are:
Correct, Safe gap, Loud failure or Silent wrong. The client has no defences on purpose, so the log
shows what each fault does to an unguarded pipeline.

`demo/scenarios/` holds one configuration per fault, and `demo/demo-run.log` is the output of
`run_demo.py`:

| Scenario | Outcome |
|---|---|
| 00 baseline (proxy, no faults) | Correct |
| 01 L1 latency 300–500 ms | Correct (slower) |
| 02 L1 latency above the client timeout | Loud failure |
| 03 L2 rate-limit error | Loud failure |
| 04 L4 call lost, never answered | Loud failure |
| 05 L5 calculator down | Safe gap |
| 06 S1 empty search · 07 S2 page cut off · 08 S3 login wall | Safe gap |
| 09 S4 last year's report · 10 S5 one figure altered · 11 S6 unofficial site first · 12 S7 wrong calculation · 13 S8 European number format | Silent wrong |
| 14 seeded random mix, run twice | Safe gap, identical both times |

### With a real LLM

`demo/llm_agent.py` replaces the script with an LLM served by any OpenAI-compatible endpoint
(`LLM_URL`, default LM Studio on `localhost:1234`; `LLM_MODEL`; `RUNS`; `PARALLEL`). The model gets
the same task and the MCP tools as functions, calls them through the same proxies, and its final
JSON summary is graded with the same answer key. `demo/llm-run.log` holds 3 runs per scenario with
Qwen3.5 9B (Q4_K_M) in LM Studio. The model recovered from every loud fault, but reported a
plausible wrong value as fact when one reached it (S5 2/3, S7 3/3). `demo/lmstudio_usage.py`
extracts the token counts from LM Studio's server log; `demo/llm-usage.json` holds them for that
batch (8.6k input and 2.8k output tokens per run).

```sh
uv run python demo/llm_agent.py            # all scenarios; or give name prefixes, e.g. 10 12
```

To run one scenario by hand: `uv run python demo/report_client.py demo/scenarios/10-S5-one-figure-altered.yaml`.
Fault logs go to `logs/`.

## Known limitations

- **stdio only.** There is no Streamable HTTP or SSE transport, so remote servers need a
  stdio bridge.
- **Only tool calls are faulted.** Faults in messages between agents, or in an approval
  step, live inside the agent framework, and an MCP proxy cannot see them.
- **L3 and L4 are approximated.** `drop` loses one call while the server keeps running. The proxy
  does not kill or restart the server, and does not send malformed JSON-RPC or cut the stream.
- **Reproducibility assumes the same call order.** Decisions follow the order in which requests
  reach the proxy. A client that issues calls concurrently, or an LLM that takes a different path,
  sees different calls hit, by design. The counters reset when the proxy restarts.
- **Matching is by tool name only.** No rule can target arguments (for example, only one company's
  URL). `nth` and `rate` select the call instead.
- **Result faults handle text and JSON content.** Image, audio and embedded-resource blocks pass
  through unchanged. A faulted response is re-serialised, so its whitespace differs from the
  server's.
- **Latency applies on the way to the server.** No fault slows the response on its way back,
  which matters only for streaming or progress notifications.
- **The scripted client is not an agent, and the LLM agent is one agent, not three.** Neither
  reproduces the message passing of a multi-agent system. With an LLM, the call order changes
  between runs, so the same seed can hit different calls: the fault schedule is reproducible, the
  outcome is not.
