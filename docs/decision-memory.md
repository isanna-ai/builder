# Decision Memory — distillation, budget/gate, dedup, pull-mode

The dispatcher can use an MCP memory service as a provider: at **plan** time it recalls prior
`decision`/`learned` memories and injects a "Prior art / known pitfalls" block; at
**verify** time it writes new decision/learned memories. This doc covers the
token-efficiency layer that sits on top of it.

## Why

An A/B measurement showed that pushing **raw** prior
art into every plan prompt cost **+43% plan tokens** — decisions were stored as verbose
prose and recalled as a verbatim top-8 dump. This layer makes decision memory
token-efficient at storage and injection, and re-proves the delta.

## Flags

All behavior changes are env flags read in the **dispatcher process** (the distiller in
`write_decision_memory` and the recall renderer in `_render_prior_art`/`build_phase_goal`).
Defaults reproduce the previous behavior exactly. Only the `[spec_id, spec_id]` tag-bug
fix and `is_duplicate` accounting are always-on.

| flag | default | meaning |
|---|---|---|
| `MEMORY_DISTILL_MODEL` | unset → identity | distill each decision to a terse rule at write time via `claude -p` (pass a Claude Code model **alias** such as `sonnet` or `haiku`; a dated model id is not accepted here) |
| `PRIOR_ART_CHAR_BUDGET` | `0` → no cap | cap the injected prior-art block, highest-relevance first |
| `PRIOR_ART_REL_GATE` | `0` → disabled | drop candidates scoring below this fraction of the top candidate's score (path-agnostic) |
| `MEMORY_SUPERSEDE` | `0` → off | before writing, delete prior memories sharing `[module, spec_id]` tags (destructive — operator decision) |
| `MEMORY_RECALL_MODE` | `push` | `push` (proactive injection) \| `pull` (agentic on-demand recall tool) \| `hybrid` (both) \| `off` |

## How it works

- **Distill-at-write** — `write_decision_memory` batches one `claude -p` call to compress
  the turn's decisions into terse imperative rules; the **raw prose is preserved** in the
  memory's `metadata.detail`, so nothing is lost. Identity fallback on any error;
  never raises.
- **Budget + relative gate** — `_render_prior_art` caps the block by characters and drops
  low-relevance candidates. `decisions_reused` reflects what was actually rendered.
- **Dedup + tag fix** — the provider's exact-hash `is_duplicate` signal is counted as
  `decisions_deduped` (not `decisions_written`); tags are `[module, spec_id]` (was a
  duplicated `[spec_id, spec_id]`). Distillation makes canonical rules hash-collide, so
  re-derived decisions dedup naturally.
- **Pull-mode** (`MEMORY_RECALL_MODE=pull`) — instead of pushing a block, the plan agent is
  given the provider's search tool over MCP and fetches prior art only when it needs it.
  Pull turns run `--output-format stream-json --verbose` so recall stats are recovered from
  the agent's tool-call records.

## Measured results

- **Deterministic** (the right tool for write-side distillation): distilling a realistic
  corpus cut the injected block **~32%**, raw recoverable from `detail`.
- **A/B** (`ab-memory-gain.py`, arm-aware report): `push-distilled` injected **191** prior-art
  tokens vs `push-raw` **436** — **−56%** at preserved recall (hit-rate 1.0).
- **Pull** validated under real plan load: `recall_calls=1, recall_hits=1, decisions_reused=10`
  per turn (was always 0 before the `stream-json` fix); `prior_art_tokens=0` (pull pushes
  nothing).

## Telemetry

`memory_eval` events gained `prior_art_tokens`, `decisions_distilled`, `decisions_deduped`,
`recall_mode` (all optional/back-compat). `ab-memory-gain.py --arms off,push-raw,push-distilled,pull`
runs an isolated (`plan_gate:true`, dedicated queue, per-arm seed isolation) benchmark and
`render_arm_report` separates the arms (which the legacy two-arm report cannot).

## Enabling it

Decision memory talks to an MCP memory service. The dispatcher does nothing at all unless the first two of these are set:

| variable | meaning |
|---|---|
| `HIVEMIND_MCP_URL` | the MCP endpoint of the memory service |
| `HIVEMIND_API_KEY` | its API key |
| `HIVEMIND_TIMEOUT_MS` | optional; request timeout in ms, default 5000. `EMBEDDING_TIMEOUT_MS` is honoured as a fallback. |

With both present, the claude lane is given the provider's search tool
(`mcp__hive__hive_search_memories`) and the flags in the table above become live. With either
missing, `_hive_client()` returns `None`, every entry point no-ops, and the flags are inert.

The tool name and variable names are those of the service this was built against; there is no
provider-agnostic adapter layer yet, so pointing it at a different MCP memory service means
matching that interface.

## Scope

Decision memory is **optional and off by default**: with no memory provider configured the
dispatcher recalls nothing, writes nothing, and every flag above is inert. Nothing in the
spec lifecycle — the phases, the host gate, the verdict — depends on it. Wiring a provider is
a separate, explicit act.
