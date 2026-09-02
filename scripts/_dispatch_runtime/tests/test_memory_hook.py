"""Task 8 — Builder plan-time recall + decision-write hook.

RED-first: assert plan_prior_art_block calls hive_search_memories with the right
shape, counts rendered (not returned) memories, degrades to empty/zero on
breaker-open / error, and write_decision_memory writes one memory per item with
tags=[module, spec_id] + source_conversation_id=spec_id.

Uses constructor-injected fake clients (the repo's local pytest runner supports
only zero-arg / tmp_path tests — no monkeypatch fixture).
"""

from __future__ import annotations

import json

from _dispatch_runtime import memory_hook


class FakeHive:
    """A fake hive client: records calls, returns canned search results."""

    def __init__(self, search_results=None, raise_on=None):
        self.search_results = search_results if search_results is not None else []
        self.raise_on = set(raise_on or ())
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool in self.raise_on:
            raise RuntimeError(f"{tool} boom")
        if tool == "bia_search_memories":
            return {"results": self.search_results}
        if tool == "bia_add_memory":
            return {"memory_id": "m-new", "is_duplicate": False}
        return {}


def test_plan_prior_art_block_searches_and_renders():
    results = [
        {"content": "use squash merges", "type": "decision"},
        {"content": "pg_trgm must be installed", "type": "learned"},
    ]
    fake = FakeHive(search_results=results)
    block, stats = memory_hook.plan_prior_art_block(
        "auth refactor", breaker_open=False, client=fake
    )

    assert len(fake.calls) == 1
    tool, args = fake.calls[0]
    assert tool == "bia_search_memories"
    assert args["query"] == "auth refactor"
    assert args["limit"] == 8
    # The decision/learned filter is applied while rendering, not via a server-side
    # `type` LIST (hive_search_memories takes only a single type string).
    assert "squash merges" in block
    assert "pg_trgm" in block
    assert stats["recall_calls"] == 1
    assert stats["recall_hits"] == 1
    assert stats["decisions_reused"] == 2


def test_plan_prior_art_block_filters_to_decision_and_learned():
    # A mixed-type result set: only decision/learned are rendered into the block.
    results = [
        {"content": "squash merges", "type": "decision"},
        {"content": "a random observation", "type": "observation"},
        {"content": "pg_trgm required", "type": "learned"},
    ]
    fake = FakeHive(search_results=results)
    block, stats = memory_hook.plan_prior_art_block("x", breaker_open=False, client=fake)
    assert "squash merges" in block
    assert "pg_trgm required" in block
    assert "random observation" not in block
    assert stats["recall_hits"] == 1  # len(results) >= 1
    assert stats["decisions_reused"] == 2  # only decision+learned rendered


def test_plan_prior_art_block_no_results_zero_hit():
    fake = FakeHive(search_results=[])
    block, stats = memory_hook.plan_prior_art_block("intent", breaker_open=False, client=fake)
    assert block == ""
    assert stats["recall_calls"] == 1
    assert stats["recall_hits"] == 0
    assert stats["decisions_reused"] == 0


def test_decisions_reused_tracks_rendered_not_returned():
    # One memory has empty content -> rendered into the block as 1, not 2.
    results = [
        {"content": "keep RLS on every table", "type": "decision"},
        {"content": "   ", "type": "learned"},
    ]
    fake = FakeHive(search_results=results)
    block, stats = memory_hook.plan_prior_art_block("x", breaker_open=False, client=fake)
    assert stats["recall_hits"] == 1  # len(results) >= 1
    assert stats["decisions_reused"] == 1  # only one actually rendered
    assert "RLS" in block


def test_breaker_open_makes_no_call():
    fake = FakeHive(search_results=[{"content": "x", "type": "decision"}])
    block, stats = memory_hook.plan_prior_art_block("intent", breaker_open=True, client=fake)
    assert block == ""
    assert stats == memory_hook.ZERO_RECALL_STATS
    assert fake.calls == []


def test_empty_intent_makes_no_call():
    fake = FakeHive(search_results=[{"content": "x", "type": "decision"}])
    block, stats = memory_hook.plan_prior_art_block("", breaker_open=False, client=fake)
    assert block == ""
    assert stats["recall_calls"] == 0
    assert fake.calls == []


def test_client_error_returns_empty_zero():
    fake = FakeHive(raise_on={"bia_search_memories"})
    block, stats = memory_hook.plan_prior_art_block("intent", breaker_open=False, client=fake)
    assert block == ""
    assert stats["recall_hits"] == 0
    assert stats["decisions_reused"] == 0


def test_no_client_configured_returns_zero():
    # client=None and (in the local runner) no HIVEMIND_* env -> off arm.
    import os

    saved = {k: os.environ.pop(k, None) for k in ("HIVEMIND_MCP_URL", "HIVEMIND_API_KEY")}
    try:
        block, stats = memory_hook.plan_prior_art_block("intent", breaker_open=False)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert block == ""
    assert stats["recall_calls"] == 0


def test_write_decision_memory_writes_per_item():
    fake = FakeHive()
    written = memory_hook.write_decision_memory("spec-x", "modA", ["d1"], ["l1"], client=fake)
    assert written == 2
    add_calls = [c for c in fake.calls if c[0] == "bia_add_memory"]
    assert len(add_calls) == 2
    by_type = {c[1]["type"]: c[1] for c in add_calls}
    assert set(by_type) == {"decision", "learned"}
    for args in by_type.values():
        # spec_id provenance is always carried in tags=[module, spec_id].
        assert args["tags"] == ["modA", "spec-x"]
        # A non-UUID spec_id is NOT sent as source_conversation_id (the live
        # schema validates it as a UUID); it is omitted.
        assert "source_conversation_id" not in args
    assert by_type["decision"]["content"] == "d1"
    assert by_type["learned"]["content"] == "l1"


def test_write_decision_memory_uuid_spec_id_sets_source_conversation_id():
    fake = FakeHive()
    sid = "11111111-2222-3333-4444-555555555555"
    written = memory_hook.write_decision_memory(sid, "modA", ["d1"], [], client=fake)
    assert written == 1
    args = [c for c in fake.calls if c[0] == "bia_add_memory"][0][1]
    assert args["source_conversation_id"] == sid
    assert args["tags"] == ["modA", sid]


def test_write_decision_memory_no_client_returns_zero():
    import os

    saved = {k: os.environ.pop(k, None) for k in ("HIVEMIND_MCP_URL", "HIVEMIND_API_KEY")}
    try:
        written = memory_hook.write_decision_memory("spec-x", "modA", ["d1"], ["l1"])
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert written == 0


def test_write_decision_memory_swallows_errors():
    fake = FakeHive(raise_on={"bia_add_memory"})
    written = memory_hook.write_decision_memory("spec-x", "modA", ["d1"], [], client=fake)
    assert written == 0


# --------------------------------------------------------------------------- #
# S6/S7 additions: distillation seam, dedup accounting, prior_art_tokens,
# char budget + relative gate. Vendored-shim rules: bare def test_*(), plain
# assert, no fixtures except tmp_path, no monkeypatch/pytest.raises.
# --------------------------------------------------------------------------- #

import os  # noqa: E402


def _pop_env(*keys):
    """Save + remove env keys; returns a dict to restore with _restore_env."""
    return {k: os.environ.pop(k, None) for k in keys}


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class RecordingHive:
    """Fake client recording every hive_add_memory arg dict; lets a per-call
    result be scripted (e.g. is_duplicate) and supports hive_memory_delete."""

    def __init__(self, results=None, raise_on=None):
        # results: list of dicts returned by successive hive_add_memory calls.
        self._results = list(results or [])
        self.raise_on = set(raise_on or ())
        self.calls: list[tuple[str, dict]] = []
        self.add_args: list[dict] = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool in self.raise_on:
            raise RuntimeError(f"{tool} boom")
        if tool == "bia_add_memory":
            self.add_args.append(args)
            if self._results:
                return self._results.pop(0)
            return {"memory_id": "m", "is_duplicate": False}
        return {}


# -- distillation seam ------------------------------------------------------- #

def test_distiller_distilled_content_and_raw_detail_sent():
    # An injected distiller that compresses each note; the written content must be
    # the distilled text and `detail` must carry the RAW text.
    fake = RecordingHive()

    def distiller(batch):
        # batch is list[(text, mtype)]; same-order list[str] out.
        return [f"D:{text}" for text, _ in batch]

    written = memory_hook.write_decision_memory(
        "spec-x", "modA", ["decided X"], ["learned Y"], client=fake, distiller=distiller
    )
    assert written == 2
    by_type = {a["type"]: a for a in fake.add_args}
    assert by_type["decision"]["content"] == "D:decided X"
    assert by_type["decision"]["detail"] == "decided X"
    assert by_type["learned"]["content"] == "D:learned Y"
    assert by_type["learned"]["detail"] == "learned Y"
    stats = memory_hook.last_write_stats()
    assert stats["written"] == 2
    assert stats["distilled"] == 2
    assert stats["deduped"] == 0


def test_distiller_failure_falls_back_to_identity():
    # A distiller that raises -> raw texts are written, NO detail key (today's shape).
    fake = RecordingHive()

    def boom(batch):
        raise RuntimeError("distill exploded")

    written = memory_hook.write_decision_memory(
        "spec-x", "modA", ["raw one"], [], client=fake, distiller=boom
    )
    assert written == 1
    args = fake.add_args[0]
    assert args["content"] == "raw one"
    assert "detail" not in args
    assert memory_hook.last_write_stats()["distilled"] == 0


def test_distiller_wrong_length_falls_back_to_identity():
    fake = RecordingHive()

    def short(batch):
        return ["only one"]  # wrong length for a 2-item batch

    written = memory_hook.write_decision_memory(
        "spec-x", "modA", ["a", "b"], [], client=fake, distiller=short
    )
    assert written == 2
    for args in fake.add_args:
        assert "detail" not in args
    assert memory_hook.last_write_stats()["distilled"] == 0


def test_default_no_flag_keeps_todays_call_shape():
    # No distiller injected + MEMORY_DISTILL_MODEL unset -> identity; no detail key.
    saved = _pop_env("MEMORY_DISTILL_MODEL")
    try:
        fake = RecordingHive()
        written = memory_hook.write_decision_memory(
            "spec-x", "modA", ["d1"], ["l1"], client=fake
        )
    finally:
        _restore_env(saved)
    assert written == 2
    for args in fake.add_args:
        assert "detail" not in args
        assert args["tags"] == ["modA", "spec-x"]
    assert memory_hook.last_write_stats()["distilled"] == 0


def test_distill_batch_identity_when_model_unset():
    saved = _pop_env("MEMORY_DISTILL_MODEL")
    try:
        out = memory_hook._distill_batch(["x", "y"])
    finally:
        _restore_env(saved)
    assert out == ["x", "y"]


def test_distill_batch_never_raises_on_empty():
    assert memory_hook._distill_batch([]) == []


# -- is_duplicate / dedup accounting ----------------------------------------- #

def test_is_duplicate_excluded_from_written_and_counted_in_deduped():
    # First add returns is_duplicate True, second is a real write.
    fake = RecordingHive(results=[
        {"memory_id": "m1", "is_duplicate": True},
        {"memory_id": "m2", "is_duplicate": False},
    ])
    written = memory_hook.write_decision_memory(
        "spec-x", "modA", ["dup", "fresh"], [], client=fake
    )
    assert written == 1  # the duplicate is NOT counted
    stats = memory_hook.last_write_stats()
    assert stats["written"] == 1
    assert stats["deduped"] == 1
    assert stats["distilled"] == 0


def test_last_write_stats_reset_each_call():
    fake = RecordingHive(results=[{"memory_id": "m", "is_duplicate": True}])
    memory_hook.write_decision_memory("spec-x", "modA", ["dup"], [], client=fake)
    assert memory_hook.last_write_stats()["deduped"] == 1
    # A fresh call with a normal write resets deduped back to 0.
    fake2 = RecordingHive()
    memory_hook.write_decision_memory("spec-y", "modB", ["ok"], [], client=fake2)
    stats = memory_hook.last_write_stats()
    assert stats["written"] == 1
    assert stats["deduped"] == 0


def test_existing_write_count_unchanged():
    # Keep the original contract: write_decision_memory returns N written.
    fake = RecordingHive()
    written = memory_hook.write_decision_memory(
        "spec-x", "modA", ["d1", "d2"], ["l1"], client=fake
    )
    assert written == 3


# -- MEMORY_SUPERSEDE -------------------------------------------------------- #

def test_supersede_deletes_before_write():
    saved = {"MEMORY_SUPERSEDE": os.environ.get("MEMORY_SUPERSEDE")}
    os.environ["MEMORY_SUPERSEDE"] = "1"
    try:
        fake = RecordingHive()
        memory_hook.write_decision_memory("spec-x", "modA", ["d1"], [], client=fake)
    finally:
        if saved["MEMORY_SUPERSEDE"] is None:
            os.environ.pop("MEMORY_SUPERSEDE", None)
        else:
            os.environ["MEMORY_SUPERSEDE"] = saved["MEMORY_SUPERSEDE"]
    tools = [c[0] for c in fake.calls]
    assert tools[0] == "bia_memory_delete"
    assert fake.calls[0][1]["tags"] == ["modA", "spec-x"]
    assert "bia_add_memory" in tools


def test_supersede_delete_error_degrades_to_write():
    saved = {"MEMORY_SUPERSEDE": os.environ.get("MEMORY_SUPERSEDE")}
    os.environ["MEMORY_SUPERSEDE"] = "1"
    try:
        fake = RecordingHive(raise_on={"bia_memory_delete"})
        written = memory_hook.write_decision_memory(
            "spec-x", "modA", ["d1"], [], client=fake
        )
    finally:
        if saved["MEMORY_SUPERSEDE"] is None:
            os.environ.pop("MEMORY_SUPERSEDE", None)
        else:
            os.environ["MEMORY_SUPERSEDE"] = saved["MEMORY_SUPERSEDE"]
    # The delete failed but the write still happened.
    assert written == 1


def test_no_supersede_makes_no_delete_by_default():
    saved = _pop_env("MEMORY_SUPERSEDE")
    try:
        fake = RecordingHive()
        memory_hook.write_decision_memory("spec-x", "modA", ["d1"], [], client=fake)
    finally:
        _restore_env(saved)
    assert all(c[0] != "bia_memory_delete" for c in fake.calls)


# -- prior_art_tokens -------------------------------------------------------- #

def test_zero_recall_stats_has_prior_art_tokens():
    assert "prior_art_tokens" in memory_hook.ZERO_RECALL_STATS
    assert memory_hook.ZERO_RECALL_STATS["prior_art_tokens"] == 0


def test_plan_prior_art_block_reports_prior_art_tokens():
    results = [{"content": "x" * 40, "type": "decision"}]
    fake = FakeHive(search_results=results)
    block, stats = memory_hook.plan_prior_art_block("intent", breaker_open=False, client=fake)
    assert "prior_art_tokens" in stats
    # ~ rendered_chars // 4; block is non-empty so tokens > 0.
    assert stats["prior_art_tokens"] == len(block) // 4
    assert stats["prior_art_tokens"] > 0


def test_prior_art_tokens_present_on_breaker_open():
    fake = FakeHive(search_results=[{"content": "x", "type": "decision"}])
    _, stats = memory_hook.plan_prior_art_block("i", breaker_open=True, client=fake)
    assert stats["prior_art_tokens"] == 0


def test_prior_art_tokens_present_on_error_path():
    fake = FakeHive(raise_on={"bia_search_memories"})
    _, stats = memory_hook.plan_prior_art_block("i", breaker_open=False, client=fake)
    assert stats["prior_art_tokens"] == 0


# -- _render_prior_art default reproduces today's render --------------------- #

def test_render_default_reproduces_todays_block():
    # With no budget/gate flags, the rendered block is byte-for-byte the legacy form.
    saved = _pop_env("PRIOR_ART_CHAR_BUDGET", "PRIOR_ART_REL_GATE")
    try:
        results = [
            {"content": "use squash merges", "type": "decision"},
            {"content": "pg_trgm must be installed", "type": "learned"},
        ]
        body, rendered = memory_hook._render_prior_art(results)
    finally:
        _restore_env(saved)
    assert body == "- [decision] use squash merges\n- [learned] pg_trgm must be installed"
    assert rendered == 2


# -- char budget ------------------------------------------------------------- #

def test_char_budget_truncates_highest_relevance_first():
    # Three rows, already relevance-ranked. A budget that fits only the first two.
    results = [
        {"content": "AAAA", "type": "decision", "score": 0.9},
        {"content": "BBBB", "type": "decision", "score": 0.8},
        {"content": "CCCC", "type": "decision", "score": 0.7},
    ]
    full, _ = memory_hook._render_prior_art(results, char_budget=0, rel_gate=0.0)
    # Each rendered line is "- [decision] XXXX" = 17 chars; two lines + 1 newline.
    line_len = len("- [decision] AAAA")
    budget = line_len * 2 + 1  # fits exactly two lines
    body, rendered = memory_hook._render_prior_art(results, char_budget=budget, rel_gate=0.0)
    assert rendered == 2
    assert "AAAA" in body and "BBBB" in body
    assert "CCCC" not in body  # weakest row dropped by the budget
    assert len(body) <= budget


def test_char_budget_via_env_default_path():
    saved = _pop_env("PRIOR_ART_CHAR_BUDGET", "PRIOR_ART_REL_GATE")
    try:
        results = [
            {"content": "AAAA", "type": "decision"},
            {"content": "BBBB", "type": "decision"},
        ]
        line_len = len("- [decision] AAAA")
        os.environ["PRIOR_ART_CHAR_BUDGET"] = str(line_len)  # only first line fits
        body, rendered = memory_hook._render_prior_art(results)
    finally:
        _restore_env(saved)
    assert rendered == 1
    assert "AAAA" in body
    assert "BBBB" not in body


def test_budget_decisions_reused_equals_rendered():
    # Through plan_prior_art_block: decisions_reused must equal the budget-cut count.
    saved = _pop_env("PRIOR_ART_REL_GATE")
    os.environ["PRIOR_ART_CHAR_BUDGET"] = str(len("- [decision] AAAA"))
    try:
        results = [
            {"content": "AAAA", "type": "decision"},
            {"content": "BBBB", "type": "decision"},
        ]
        fake = FakeHive(search_results=results)
        block, stats = memory_hook.plan_prior_art_block("i", breaker_open=False, client=fake)
    finally:
        os.environ.pop("PRIOR_ART_CHAR_BUDGET", None)
        _restore_env(saved)
    # Only one row survives the budget; decisions_reused tracks the rendered count.
    assert stats["decisions_reused"] == 1
    assert "AAAA" in block and "BBBB" not in block


# -- relative gate ----------------------------------------------------------- #

def test_rel_gate_drops_weak_rows():
    results = [
        {"content": "strong", "type": "decision", "score": 1.0},
        {"content": "mid", "type": "decision", "score": 0.6},
        {"content": "weak", "type": "decision", "score": 0.2},
    ]
    # gate 0.5 -> threshold 0.5 * 1.0 = 0.5; drops the 0.2 row, keeps 1.0 and 0.6.
    body, rendered = memory_hook._render_prior_art(results, char_budget=0, rel_gate=0.5)
    assert "strong" in body
    assert "mid" in body
    assert "weak" not in body
    assert rendered == 2


def test_rel_gate_via_env():
    saved = _pop_env("PRIOR_ART_CHAR_BUDGET")
    os.environ["PRIOR_ART_REL_GATE"] = "0.5"
    try:
        results = [
            {"content": "strong", "type": "decision", "score": 1.0},
            {"content": "weak", "type": "decision", "score": 0.1},
        ]
        body, rendered = memory_hook._render_prior_art(results)
    finally:
        os.environ.pop("PRIOR_ART_REL_GATE", None)
        _restore_env(saved)
    assert "strong" in body
    assert "weak" not in body
    assert rendered == 1


def test_rel_gate_disabled_keeps_all_rows():
    # gate 0.0 (default) -> no rows dropped even with disparate scores.
    results = [
        {"content": "strong", "type": "decision", "score": 1.0},
        {"content": "weak", "type": "decision", "score": 0.01},
    ]
    body, rendered = memory_hook._render_prior_art(results, char_budget=0, rel_gate=0.0)
    assert "strong" in body and "weak" in body
    assert rendered == 2


def test_rel_gate_no_scores_keeps_all():
    # No `score` keys -> gate cannot compute a threshold -> all rows kept.
    results = [
        {"content": "a", "type": "decision"},
        {"content": "b", "type": "decision"},
    ]
    body, rendered = memory_hook._render_prior_art(results, char_budget=0, rel_gate=0.9)
    assert rendered == 2
    assert "a" in body and "b" in body


# --- _parse_distill_output: real claude -p envelopes (regression for the
# Markdown-code-fence case that plain json.loads rejects) -------------------

def test_parse_distill_output_strips_markdown_json_fence():
    # Real `claude -p --output-format json` wraps the answer in {"result": "..."}
    # and the model fences the array in ```json ... ```.
    envelope = json.dumps(
        {"result": "```json\n[\n  \"rule one\",\n  \"rule two\"\n]\n```", "is_error": False}
    )
    out = memory_hook._parse_distill_output(envelope, 2)
    assert out == ["rule one", "rule two"]


def test_parse_distill_output_bare_fence_and_prose():
    assert memory_hook._coerce_json_array("```\n[\"x\"]\n```") == ["x"]
    # leading prose + array still recovered via bracket extraction
    assert memory_hook._coerce_json_array('here you go: ["a","b"] done') == ["a", "b"]
    # plain array unchanged
    assert memory_hook._coerce_json_array('["a"]') == ["a"]
    # nothing parseable -> None
    assert memory_hook._coerce_json_array("not json at all") is None


def test_parse_distill_output_error_envelope_is_identity_signal():
    # 404/model-not-found returns is_error True -> None (caller keeps raw text)
    envelope = json.dumps({"result": "model not found", "is_error": True})
    assert memory_hook._parse_distill_output(envelope, 1) is None


# --------------------------------------------------------------------------- #
# QW2 agent-lift-observability: _HiveClient sends client_name="builder"
# Vendored-shim rules: bare def test_*(), plain assert, no monkeypatch/
# pytest.raises — swap urllib.request.urlopen manually and restore in finally.
# --------------------------------------------------------------------------- #

def test_hive_client_call_sends_client_name_builder():
    """_HiveClient.call must include client_name=="builder" in params."""
    import io
    import urllib.request as _urllib_req

    captured = []

    class _FakeResponse:
        def __init__(self):
            self._data = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"json": {"is_duplicate": False}}]},
            }).encode("utf-8")

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _fake_urlopen(request, timeout=None):
        captured.append(request)
        return _FakeResponse()

    original_urlopen = memory_hook.urllib.request.urlopen
    memory_hook.urllib.request.urlopen = _fake_urlopen
    try:
        client = memory_hook._HiveClient("http://x/mcp", "k")
        client.call("bia_add_memory", {"content": "x", "type": "decision"})
    finally:
        memory_hook.urllib.request.urlopen = original_urlopen

    assert len(captured) == 1
    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["params"]["client_name"] == "builder"
    assert body["params"]["arguments"] == {"content": "x", "type": "decision"}


def test_hive_client_call_arguments_unchanged():
    """arguments in params must be byte-identical to what was passed in."""
    import urllib.request as _urllib_req

    captured = []

    class _FakeResponse:
        def read(self):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"json": {}}]},
            }).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _fake_urlopen(request, timeout=None):
        captured.append(request)
        return _FakeResponse()

    original_urlopen = memory_hook.urllib.request.urlopen
    memory_hook.urllib.request.urlopen = _fake_urlopen
    try:
        client = memory_hook._HiveClient("http://x/mcp", "k")
        client.call("bia_search_memories", {"query": "auth refactor", "limit": 8})
    finally:
        memory_hook.urllib.request.urlopen = original_urlopen

    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["params"]["arguments"] == {"query": "auth refactor", "limit": 8}
    assert body["params"]["client_name"] == "builder"
    assert body["params"]["name"] == "bia_search_memories"


# --------------------------------------------------------------------------- #
# QW5b — canonical agent_id on write_decision_memory (agent-lift-write-normalization)
# Shim rules: bare test_*(), plain assert, no monkeypatch/pytest.raises.
# Save/restore MEMORY_AGENT_ID in try/finally.
# --------------------------------------------------------------------------- #

def test_agent_id_flag_on_codex_lane_sets_builder_codex():
    """MEMORY_AGENT_ID=1 + lane="codex-cli" => agent_id=="builder-codex", use_policy=="shared"."""
    saved = _pop_env("MEMORY_AGENT_ID")
    os.environ["MEMORY_AGENT_ID"] = "1"
    try:
        fake = RecordingHive()
        written = memory_hook.write_decision_memory(
            "spec-x", "modA", ["d1"], ["l1"], client=fake, lane="codex-cli"
        )
    finally:
        _restore_env(saved)
    assert written == 2
    for args in fake.add_args:
        assert args.get("agent_id") == "builder-codex"
        assert args.get("use_policy") == "shared"


def test_agent_id_flag_on_none_lane_defaults_to_builder_claude():
    """MEMORY_AGENT_ID=1 + lane=None => agent_id=="builder-claude" (default lane)."""
    saved = _pop_env("MEMORY_AGENT_ID")
    os.environ["MEMORY_AGENT_ID"] = "1"
    try:
        fake = RecordingHive()
        written = memory_hook.write_decision_memory(
            "spec-x", "modA", ["d1"], [], client=fake, lane=None
        )
    finally:
        _restore_env(saved)
    assert written == 1
    assert fake.add_args[0].get("agent_id") == "builder-claude"
    assert fake.add_args[0].get("use_policy") == "shared"


def test_agent_id_flag_off_no_new_args():
    """MEMORY_AGENT_ID unset => no agent_id, no use_policy (byte-identical to today)."""
    saved = _pop_env("MEMORY_AGENT_ID")
    try:
        fake = RecordingHive()
        written = memory_hook.write_decision_memory(
            "spec-x", "modA", ["d1"], ["l1"], client=fake, lane="codex-cli"
        )
    finally:
        _restore_env(saved)
    assert written == 2
    for args in fake.add_args:
        assert "agent_id" not in args
        assert "use_policy" not in args


def test_agent_id_flag_on_claude_lane_sets_builder_claude():
    """MEMORY_AGENT_ID=1 + lane="claude" => agent_id=="builder-claude"."""
    saved = _pop_env("MEMORY_AGENT_ID")
    os.environ["MEMORY_AGENT_ID"] = "1"
    try:
        fake = RecordingHive()
        memory_hook.write_decision_memory(
            "spec-x", "modA", ["d1"], [], client=fake, lane="claude"
        )
    finally:
        _restore_env(saved)
    assert fake.add_args[0].get("agent_id") == "builder-claude"
    assert fake.add_args[0].get("use_policy") == "shared"
