"""Builder plan-time decision-memory read/write hook (R5).

Two entry points used by the dispatch runtime:

  * ``plan_prior_art_block(intent, *, breaker_open)`` — reads prior decision/learned
    memories for the plan goal and renders a "Prior art / known pitfalls" block.
    Returns ``(block_text, recall_stats)``. On an empty intent, an open breaker, a
    missing endpoint, or ANY client error it returns ``("", ZERO_RECALL_STATS)`` —
    it NEVER raises, so plan-goal building always proceeds (R5 IF clause).
  * ``write_decision_memory(spec_id, module, decisions, learned)`` — post-verify
    write: one ``type=decision`` memory per decision and one ``type=learned`` per
    verify failure, each tagged ``[module, spec_id]`` with
    ``source_conversation_id=spec_id``. Returns the count actually written.

A small ``_hive_client()`` factory reads ``HIVEMIND_MCP_URL`` / ``HIVEMIND_API_KEY``
from the environment and returns ``None`` when unset (the ``memory_mode="off"``
baseline arm — recall/write are skipped, telemetry still emits with zero counts).

``decisions_reused`` is the count of memories ACTUALLY RENDERED into the block,
tracked during render (NOT ``len(results)``), so any cap/truncation is reflected.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

# hivemind's memory schema requires source_conversation_id to be a UUID. A
# Builder spec_id is a slug, not a UUID, so we only attach it when it happens to
# be UUID-shaped; otherwise the spec_id provenance is carried in `tags` (which
# always include the spec_id) and source_conversation_id is omitted.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Frozen zero-stats returned when no search is issued (breaker open / empty intent
# / no endpoint). recall_calls=0 keeps the baseline arm capturable (R5 WHERE).
ZERO_RECALL_STATS: dict[str, int] = {
    "recall_calls": 0,
    "recall_hits": 0,
    "recall_latency_ms": 0,
    "decisions_reused": 0,
    "prior_art_tokens": 0,
}

# Module-global breakdown of the last write_decision_memory call, exposed via
# last_write_stats(). Reset at the top of every write_decision_memory call and
# populated during the write loop. Independent of the int return (= written).
_LAST_WRITE_STATS: dict[str, int] = {"written": 0, "deduped": 0, "distilled": 0}

# Distillation CLI timeout (seconds). One short claude -p turn; kept well under the
# phase timeout so a slow/hung distiller degrades to identity rather than stalling.
_DISTILL_TIMEOUT_S = 120


def last_write_stats() -> dict[str, int]:
    """Breakdown of the LAST write_decision_memory call: how many memories were
    written, deduped (is_duplicate), and distilled. Reset at the top of every
    write_decision_memory; a fresh process reports zeros."""
    return dict(_LAST_WRITE_STATS)


def _distill_batch(items: list[str]) -> list[str]:
    """Compress a batch of decision/learned texts via ONE headless ``claude -p``
    turn using the model named by ``MEMORY_DISTILL_MODEL``. Returns a list the SAME
    length and order as ``items``.

    Identity fallback (returns ``items`` unchanged) when ``MEMORY_DISTILL_MODEL`` is
    unset, the CLI errors / times out, or the response cannot be parsed into a
    list of exactly len(items) strings. NEVER raises.
    """
    if not items:
        return list(items)
    model = os.environ.get("MEMORY_DISTILL_MODEL")
    if not model:
        return list(items)
    try:
        # Lazy import to avoid an import cycle (lane_claude_code_cli imports the
        # dispatch runtime which can pull this module in turn).
        from _dispatch_runtime.lane_claude_code_cli import _scrubbed_env
        from _dispatch_runtime.lane_common import run_cli_turn

        prompt = (
            "Distill each of the following decision/learned notes into a single "
            "terse, self-contained sentence that preserves the concrete fact. "
            "Return ONLY a JSON array of strings, same length and order as the "
            "input, no commentary.\n\nNOTES:\n"
            + json.dumps(list(items))
        )
        command = [
            "claude", "-p", prompt,
            "--model", model,
            "--output-format", "json",
        ]
        returncode, stdout, stderr, timed_out = run_cli_turn(
            command, cwd=os.getcwd(), env=_scrubbed_env(), timeout=_DISTILL_TIMEOUT_S,
        )
        if timed_out:
            return list(items)
        distilled = _parse_distill_output(stdout, len(items))
        if distilled is None:
            return list(items)
        return distilled
    except Exception:  # noqa: BLE001 - distillation is best-effort; degrade to identity
        return list(items)


def _parse_distill_output(stdout: str, expected: int) -> list[str] | None:
    """Extract the distilled list from a ``claude -p --output-format json`` turn.

    The CLI wraps the model's answer in an envelope ``{"result": "...", ...}``; the
    answer itself should be a JSON array of strings. Returns a list of exactly
    ``expected`` strings, or None when anything doesn't line up (caller -> identity).
    """
    text = (stdout or "").strip()
    if not text:
        return None
    payload: Any = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    # Unwrap the claude -p envelope if present; otherwise treat the parse as the
    # answer directly (lets tests/fakes return a bare array).
    answer: Any = payload
    if isinstance(payload, dict):
        if payload.get("is_error"):
            return None
        answer = payload.get("result", payload)
    if isinstance(answer, str):
        answer = _coerce_json_array(answer)
        if answer is None:
            return None
    if not isinstance(answer, list) or len(answer) != expected:
        return None
    out: list[str] = []
    for item in answer:
        if not isinstance(item, str):
            return None
        out.append(item)
    return out


def _coerce_json_array(text: str) -> Any:
    """Parse a JSON array out of a model answer string. Real ``claude -p`` answers
    often wrap the array in a Markdown code fence (```json ... ```) or add stray
    prose, which plain ``json.loads`` rejects. Tries, in order: direct parse, fence
    stripping, then first-'['..last-']' extraction. Returns the parsed value (a
    list on success) or None."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # Strip a leading ``` or ```json fence and the trailing ``` fence.
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        try:
            return json.loads(t.strip())
        except json.JSONDecodeError:
            pass
    # Last resort: extract the outermost [ ... ] span.
    start, end = t.find("["), t.rfind("]")
    if 0 <= start < end:
        try:
            return json.loads(t[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None

_PRIOR_ART_LIMIT = 8
# R5 wants decision+learned prior art. hivemind's hive_search_memories filters by
# a SINGLE `type` string (it cannot take a list), so we recall WITHOUT a
# server-side type filter and keep only these types while rendering the block.
_PRIOR_ART_TYPES = ("decision", "learned")


def _actor_user() -> str:
    """The operator behind the dispatcher (memory-actor-provenance). Defaults to the
    neutral local identity; HIVE_CLAUDE_USER / MEMORY_ACTOR_USER override. The server
    honors the claim only if the key carries actor:*."""
    import re
    raw = (os.environ.get("HIVE_CLAUDE_USER") or os.environ.get("MEMORY_ACTOR_USER")
           or "local")
    return re.sub(r"[^A-Za-z0-9._:-]", "-", raw)[:128]


def _actor_device() -> str:
    """The machine + host/container class running the dispatcher, e.g.
    'hostname:container'. Sanitized to the server's agent-id charset."""
    import re
    import socket
    try:
        host = socket.gethostname() or ""
    except Exception:  # noqa: BLE001
        host = ""
    cls = "container" if os.path.exists("/.dockerenv") else "host"
    raw = f"{host}:{cls}" if host else ""
    return re.sub(r"[^A-Za-z0-9._:-]", "-", raw)[:128]


class _HiveClient:
    """Minimal /mcp/message JSON-RPC POST client (stdlib only)."""

    def __init__(self, url: str, api_key: str, *, timeout_ms: int = 5000) -> None:
        self._endpoint = self._message_endpoint(url)
        self._api_key = api_key
        self._timeout_s = max(timeout_ms, 0) / 1000.0
        self._id = 0

    @staticmethod
    def _message_endpoint(url: str) -> str:
        base = url.rstrip("/")
        if base.endswith("/mcp/message"):
            return base
        if base.endswith("/mcp"):
            return base + "/message"
        return base + "/mcp/message"

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args, "client_name": "builder",
                       "user": _actor_user(), "device": _actor_device()},
        }
        request = urllib.request.Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s or None) as response:  # noqa: S310
            body = response.read().decode("utf-8")
        parsed = json.loads(body)
        error = parsed.get("error")
        if error:
            raise RuntimeError(str(error.get("message") or "MCP error"))
        result = parsed.get("result") or {}
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict) and "json" in content[0]:
            return content[0]["json"]
        return result


def _hive_client() -> _HiveClient | None:
    """Build a hive client from the environment, or None when no endpoint is
    configured (the ``memory_mode='off'`` baseline arm)."""
    url = os.environ.get("HIVEMIND_MCP_URL")
    api_key = os.environ.get("HIVEMIND_API_KEY")
    if not url or not api_key:
        return None
    timeout_ms = int(
        os.environ.get("HIVEMIND_TIMEOUT_MS")
        or os.environ.get("EMBEDDING_TIMEOUT_MS")
        or 5000
    )
    return _HiveClient(url, api_key, timeout_ms=timeout_ms)


def _render_prior_art(
    results: list[dict[str, Any]],
    *,
    char_budget: int | None = None,
    rel_gate: float | None = None,
) -> tuple[str, int]:
    """Render decision/learned memories into the prior-art block body. Returns
    ``(block_body, rendered_count)`` — rendered_count counts ONLY rows actually
    appended to the block, so it never drifts from len(results) when a memory has
    empty content, a non-target type, is gated out, or is dropped by the budget.

    Only ``decision``/``learned`` memories are rendered (R5): the server-side
    search cannot filter on a type LIST, so the type filter is applied here.

    Budget / gate (both default to today's behavior when their env vars are unset):
      * ``char_budget`` — when None, read ``PRIOR_ART_CHAR_BUDGET`` (default 0). A
        value > 0 caps the rendered block: rows are appended highest-relevance-first
        (results are already relevance-ranked) and appending stops once adding the
        next row would exceed the budget. 0 ⇒ no budget (today).
      * ``rel_gate`` — when None, read ``PRIOR_ART_REL_GATE`` (default 0.0). A value
        > 0 drops any row whose ``score`` is below ``rel_gate`` * the FIRST row's
        score (path-agnostic fraction of the top score). 0.0 ⇒ disabled (today).
    """
    if char_budget is None:
        try:
            char_budget = int(os.environ.get("PRIOR_ART_CHAR_BUDGET") or 0)
        except (TypeError, ValueError):
            char_budget = 0
    if rel_gate is None:
        try:
            rel_gate = float(os.environ.get("PRIOR_ART_REL_GATE") or 0.0)
        except (TypeError, ValueError):
            rel_gate = 0.0

    # Relative gate threshold is a fraction of the FIRST row's score (results are
    # already relevance-ranked). Computed once; only applied when rel_gate > 0 and
    # a usable top score exists.
    threshold: float | None = None
    if rel_gate and rel_gate > 0 and results:
        top_score = _row_score(results[0])
        if top_score is not None:
            threshold = rel_gate * top_score

    lines: list[str] = []
    rendered = 0
    used = 0
    for row in results:
        content = str(row.get("content", "")).strip()
        if not content:
            continue
        mtype = str(row.get("type", "")).strip()
        if mtype and mtype not in _PRIOR_ART_TYPES:
            continue
        if threshold is not None:
            score = _row_score(row)
            if score is not None and score < threshold:
                continue
        prefix = f"[{mtype}] " if mtype else ""
        line = f"- {prefix}{content}"
        if char_budget and char_budget > 0:
            # Account for the newline joining this line to the previous one so the
            # final rendered block stays within budget. Stop at the first row that
            # would overflow (highest-relevance-first ⇒ weakest rows drop first).
            added = len(line) + (1 if lines else 0)
            if used + added > char_budget:
                break
            used += added
        lines.append(line)
        rendered += 1
    return "\n".join(lines), rendered


def _row_score(row: dict[str, Any]) -> float | None:
    """Best-effort numeric relevance score for a search row, or None when absent."""
    raw = row.get("score")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def plan_prior_art_block(
    intent: str, *, breaker_open: bool, client: Any | None = None
) -> tuple[str, dict[str, int]]:
    """Recall prior decisions/learned for ``intent`` and render the prior-art block.

    Returns ``(block_text, recall_stats)``. Degrades to ``("", ZERO_RECALL_STATS)``
    on empty intent, open breaker, missing endpoint, or any error — never raises.

    ``client`` is injectable for tests; when None it is resolved from the env via
    ``_hive_client()`` (None ⇒ no endpoint configured ⇒ the off baseline arm).
    """
    intent = (intent or "").strip()
    if breaker_open or not intent:
        return "", dict(ZERO_RECALL_STATS)

    if client is None:
        client = _hive_client()
    if client is None:
        return "", dict(ZERO_RECALL_STATS)

    t0 = time.monotonic()
    try:
        # hive_search_memories filters on a single `type` string only, so we omit
        # the (decision|learned) type filter here and apply it while rendering the
        # block (see _render_prior_art); recall_hits still reflects the raw search.
        payload = client.call(
            "bia_search_memories",
            {"query": intent, "limit": _PRIOR_ART_LIMIT},
        )
    except Exception:  # noqa: BLE001 - recall failure must not fail the plan phase
        return "", {
            "recall_calls": 1,
            "recall_hits": 0,
            "recall_latency_ms": int((time.monotonic() - t0) * 1000),
            "decisions_reused": 0,
            "prior_art_tokens": 0,
        }
    latency_ms = int((time.monotonic() - t0) * 1000)

    results = payload.get("results", []) if isinstance(payload, dict) else []
    body, rendered = _render_prior_art(results)
    stats = {
        "recall_calls": 1,
        "recall_hits": 1 if len(results) >= 1 else 0,
        "recall_latency_ms": latency_ms,
        "decisions_reused": rendered,
        # Approximate token count of the rendered block (~4 chars/token). Truthful
        # after any budget cut because `body` is the post-budget rendered text.
        "prior_art_tokens": len(body) // 4,
    }
    if rendered == 0:
        return "", stats
    return body, stats


def write_decision_memory(
    spec_id: str,
    module: str,
    decisions: list[str],
    learned: list[str],
    *,
    client: Any | None = None,
    distiller: Any | None = None,
    lane: str | None = None,
) -> int:
    """Write one ``type=decision`` memory per decision and one ``type=learned`` per
    verify failure. tags=[module, spec_id]; source_conversation_id=spec_id.
    Returns the number actually written (NOT counting is_duplicate dedups). Never
    raises.

    Side effect: records a breakdown in the module-global ``last_write_stats()``
    (``{"written","deduped","distilled"}``), reset at the top of this call and
    populated during the write loop.

    ``client`` is injectable for tests; None ⇒ resolved from env (None ⇒ off arm).

    ``distiller`` is an optional callable ``list[(text, mtype)] -> list[str]`` (same
    order) that compresses the batch in ONE turn. Default None ⇒ resolve to the
    MEMORY_DISTILL_MODEL-gated batch distiller, or identity when the flag is unset.
    On any distiller error the raw texts are used (identity). When a distilled text
    differs from its raw text, the distilled text is written as ``content`` and the
    RAW text is passed as ``detail``; when NOT distilling (identity) NO ``detail`` is
    sent (keeps today's call shape so the live server is unaffected with the flag
    off). When ``MEMORY_SUPERSEDE`` == "1", existing memories tagged [module,
    spec_id] are deleted before the write loop (degrades to skip on error).

    ``lane`` is the dispatch lane (e.g. "claude", "codex-cli", None). When the
    ``MEMORY_AGENT_ID`` env flag is "1", every ``hive_add_memory`` write carries
    ``agent_id="builder-<lane_kind>"`` (``lane_kind`` is "codex" if "codex" is
    in the lane string, else "claude") and ``use_policy="shared"``. When the flag
    is unset the args shape is byte-identical to today."""
    _LAST_WRITE_STATS.update({"written": 0, "deduped": 0, "distilled": 0})

    # 5b flag-gate: compose canonical agent_id only when MEMORY_AGENT_ID=1.
    _agent_id_enabled = os.environ.get("MEMORY_AGENT_ID") == "1"
    _lane_kind = "codex" if "codex" in str(lane or "").lower() else "claude"
    _aid = f"builder-{_lane_kind}" if _agent_id_enabled else None

    if client is None:
        client = _hive_client()
    if client is None:
        return 0

    tags = [module, spec_id]

    # Build the ordered batch of (raw_text, mtype), skipping empty entries up front
    # so the distiller index lines up 1:1 with what we write.
    batch: list[tuple[str, str]] = []
    for content, mtype in (
        *((d, "decision") for d in (decisions or [])),
        *((l, "learned") for l in (learned or [])),
    ):
        text = str(content or "").strip()
        if not text:
            continue
        batch.append((text, mtype))

    if not batch:
        return 0

    # Optional supersede: delete prior memories for this [module, spec_id] before
    # writing the fresh batch. Best-effort — a delete failure must not block writes.
    if os.environ.get("MEMORY_SUPERSEDE") == "1":
        try:
            client.call("bia_memory_delete", {"tags": [module, spec_id]})
        except Exception:  # noqa: BLE001 - supersede is best-effort
            pass

    # Distill the WHOLE batch once. Identity (raw == distilled) by default and on
    # any error, so the off path is byte-for-byte today's behavior.
    raws = [text for text, _ in batch]
    if distiller is None:
        distilled = _distill_batch(raws)
    else:
        try:
            distilled = distiller(list(batch))
        except Exception:  # noqa: BLE001 - distiller is best-effort; degrade to identity
            distilled = list(raws)
    # Guard against a misbehaving distiller (wrong length / non-list) -> identity.
    if not isinstance(distilled, list) or len(distilled) != len(raws):
        distilled = list(raws)

    has_source = bool(_UUID_RE.match(spec_id))
    written = 0
    for (raw_text, mtype), distilled_text in zip(batch, distilled):
        out_text = distilled_text if isinstance(distilled_text, str) else raw_text
        is_distilled = out_text != raw_text
        args: dict[str, Any] = {"content": out_text, "type": mtype, "tags": tags}
        # Only attach source_conversation_id when spec_id is UUID-shaped (the live
        # schema validates it as a UUID); the spec_id provenance is always in tags.
        if has_source:
            args["source_conversation_id"] = spec_id
        # Carry the raw text as `detail` ONLY when we actually distilled, so the
        # off/identity path keeps today's exact call shape.
        if is_distilled:
            args["detail"] = raw_text
        # 5b flag-gate: set canonical agent_id + use_policy only when enabled.
        # When the flag is unset, args is byte-identical to today.
        if _aid is not None:
            args["agent_id"] = _aid
            args["use_policy"] = "shared"
        try:
            res = client.call("bia_add_memory", args)
        except Exception:  # noqa: BLE001 - a write failure must not fail the turn
            continue
        if isinstance(res, dict) and res.get("is_duplicate"):
            _LAST_WRITE_STATS["deduped"] += 1
            continue
        written += 1
        if is_distilled:
            _LAST_WRITE_STATS["distilled"] += 1

    _LAST_WRITE_STATS["written"] = written
    return written
