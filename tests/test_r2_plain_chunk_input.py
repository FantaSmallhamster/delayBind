import asyncio
from pathlib import Path

import pytest

from delaybind_core.cursor_plain_r2 import PlainTokenChunkCursor
from delaybind_core.prompts_r2 import messages
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.schema_r2 import EvidencePlanR2
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.tokenization import TokenizerJSON


class CharacterTokenizer:
    name_or_path = "test-character-tokenizer"

    def encode(self, text):
        return [ord(c) for c in text]

    def decode(self, tokens):
        return "".join(map(chr, tokens))


def plan():
    return EvidencePlanR2(plan_id="plain", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher")])


class NoneClient:
    def __init__(self, *, hint=False):
        self.calls = []
        self.hint = hint

    async def complete(self, *, interface, messages, **kwargs):
        self.calls.append((interface, messages))
        if interface == "PLAN":
            return "garbled repair"  # Only used for optional REPAIR in these tests.
        if interface == "UPDATE":
            if self.hint and sum(kind == "UPDATE" for kind, _ in self.calls) == 1:
                return "PLAN_HINT | Cindy used a different name and alias evidence is needed."
            return "NONE"
        if interface == "ANSWER":
            return "\\boxed{UNKNOWN}"
        raise AssertionError(interface)


@pytest.mark.parametrize("length,size", [(4, 5), (5, 5), (6, 5), (21, 5)])
def test_cursor_matches_reference_token_slices(length, size):
    text = "  " + "x" * length + "  "
    tokenizer = CharacterTokenizer()
    reference = tokenizer.encode(text.strip())
    cursor = PlainTokenChunkCursor(text, tokenizer, chunk_size=size)
    chunks = []
    while not cursor.exhausted:
        chunks.append(cursor.next_chunk())
    assert [c.chunk_text for c in chunks] == [tokenizer.decode(reference[i:i + size])
                                                for i in range(0, len(reference), size)]
    assert [(c.token_start, c.token_end) for c in chunks] == [
        (i, min(i + size, len(reference))) for i in range(0, len(reference), size)]
    resumed = PlainTokenChunkCursor(text, tokenizer, chunk_size=size, state=cursor.state())
    assert resumed.exhausted and resumed.window_index == len(chunks)


def test_real_tokenizer_uses_exact_reference_decode():
    path = Path(__file__).parents[1] / "models/Qwen3.5-9B-tokenizer/tokenizer.json"
    tokenizer = TokenizerJSON(path)
    text = "  Document 7:\nA A\nDocument 2:\nA A 🌍\nDocument 10: done.  "
    tokens = tokenizer.encode(text.strip())
    cursor = PlainTokenChunkCursor(text, tokenizer, chunk_size=7)
    actual = []
    while not cursor.exhausted:
        actual.append(cursor.next_chunk().chunk_text)
    assert actual == [tokenizer.decode(tokens[i:i + 7]) for i in range(0, len(tokens), 7)]


def test_actual_requests_and_snapshots_contain_identical_sections():
    context = "  Document 7: repeated\nDocument 2: repeated\nDocument 10: end  "
    question = "GLOBAL_QUESTION_SENTINEL: Compare the two family trees."
    client, tokenizer, store = NoneClient(), CharacterTokenizer(), SQLiteEventStore()
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=15)
    result = asyncio.run(V5Runner(client, config=config, tokenizer=tokenizer).run(
        run_id="plain-snapshots", question=question, context=context, store=store, plan=plan()))
    reference = tokenizer.encode(context.strip())
    chunks = [tokenizer.decode(reference[i:i + 15]) for i in range(0, len(reference), 15)]
    snapshots = [m for m in result["context_manifests"] if m.get("interface") == "UPDATE_SNAPSHOT"]
    calls = [messages for interface, messages in client.calls if interface == "UPDATE"]
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    assert [m["chunk_text"] for m in snapshots] == chunks
    assert len(calls) == len(chunks)
    for index, message in enumerate(calls):
        assert "<section>\n" + chunks[index] + "\n</section>" in message[-1]["content"]
        assert "<DOCUMENT_BOUNDARY>" not in message[-1]["content"]
        assert all(question not in part["content"] for part in message)
        assert "<problem>" not in message[-1]["content"]
    assert result["r2_metrics"]["chunk_body_tokens"] == len(reference)
    assert result["r2_metrics"]["transaction_replay_consistency"] == 1
    assert all(m["raw_input_tokens"] == len(chunks[i]) for i, m in enumerate(
        m for m in result["context_manifests"] if m.get("interface") == "UPDATE" and m.get("kind") == "MODEL_REQUEST"))


def test_optional_bad_plan_repair_is_recorded_once_across_later_chunks_and_resume():
    client, store = NoneClient(hint=True), SQLiteEventStore()
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=50,
                          plan_repair_mode="on_hint", plan_repair_failure_policy="continue_valid_plan",
                          max_protocol_retries=1)
    runner = V5Runner(client, config=config, tokenizer=CharacterTokenizer())
    context = "Document 1: A" + " " * 45 + "Document 2: B"
    first = asyncio.run(runner.run(run_id="rejected", question="Who teaches Cindy?",
                                   context=context, store=store, plan=plan()))
    assert first["status"] == "INSUFFICIENT", first["reason_codes"]
    assert first["interface_calls"]["PLAN"] == 2
    assert len(first["state"]["plan_repair_failures"]) == 1
    assert first["state"]["processed_hints"] == []
    assert first["state"]["hints"]
    assert first["r2_metrics"]["transaction_replay_consistency"] == 1
    before = len(client.calls)
    second = asyncio.run(runner.run(run_id="rejected", question="Who teaches Cindy?",
                                    context=context, store=store, plan=plan()))
    assert len(client.calls) == before and second["state"] == first["state"]


def test_optional_repair_policy_also_continues_legacy_archive_windows():
    client, store = NoneClient(hint=True), SQLiteEventStore()
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          chunk_size=1000, plan_repair_mode="on_hint",
                          plan_repair_failure_policy="continue_valid_plan",
                          max_protocol_retries=1)
    result = asyncio.run(V5Runner(client, config=config, tokenizer=CharacterTokenizer()).run(
        run_id="legacy-optional", question="Who teaches Cindy?",
        context="Document 1: Cindy's teacher is unknown.", store=store, plan=plan()))
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    assert result["r2_metrics"]["plan_repair_rejected_count"] == 1
    assert result["r2_metrics"]["transaction_replay_consistency"] == 1


def test_rejected_optional_repair_still_drains_ready_memory_and_answers():
    from delaybind_core.smoke_r2 import ScriptedR2Client

    class BoundClient(ScriptedR2Client):
        async def complete(self, *, interface, messages, **kwargs):
            if interface == "UPDATE":
                self.calls.append((interface, {"body": messages[-1]["content"]}, kwargs.get("local_metadata")))
                return "Q1 | Cindy's teacher is Alice.\nPLAN_HINT | Alias evidence is missing."
            if interface == "PLAN":
                self.calls.append((interface, {"body": messages[-1]["content"]}, kwargs.get("local_metadata")))
                return "invalid repair"
            if interface == "ANSWER":
                self.calls.append((interface, {"body": messages[-1]["content"]}, kwargs.get("local_metadata")))
                return "\\boxed{Alice}"
            return await super().complete(interface=interface, messages=messages, **kwargs)

    client = BoundClient()
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=1000,
                          plan_repair_mode="on_hint", plan_repair_failure_policy="continue_valid_plan",
                          max_protocol_retries=1)
    result = asyncio.run(V5Runner(client, config=config, tokenizer=CharacterTokenizer()).run(
        run_id="repair-then-bind", question="Who teaches Cindy?",
        context="Cindy's teacher is Alice.", store=SQLiteEventStore(), plan=plan()))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["answer"]["answer"] == "Alice"
    assert result["state"]["bindings"] == {"?teacher": "Alice"}
    assert result["r2_metrics"]["plan_repair_rejected_count"] == 1
    assert result["interface_calls"]["MEMORY"] >= 1
    event_names = [e["event_type"] for e in result["events"]]
    assert event_names.index("PLAN_REPAIR_REJECTED") < event_names.index("BINDING_CREATED")


@pytest.mark.parametrize("repairs", [("invalid repair", "NONE"), ("NONE",)])
def test_valid_repair_retry_or_none_is_success_without_rejection(repairs):
    class RecoverClient(NoneClient):
        pending = list(repairs)

        async def complete(self, *, interface, messages, **kwargs):
            if interface == "PLAN":
                self.calls.append((interface, messages))
                return self.pending.pop(0)
            return await super().complete(interface=interface, messages=messages, **kwargs)

    client = RecoverClient(hint=True)
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=1000,
                          plan_repair_mode="on_hint", plan_repair_failure_policy="continue_valid_plan",
                          max_protocol_retries=1)
    result = asyncio.run(V5Runner(client, config=config, tokenizer=CharacterTokenizer()).run(
        run_id="valid-" + str(len(repairs)), question="Who teaches Cindy?",
        context="Alias evidence may be missing.", store=SQLiteEventStore(), plan=plan()))
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    assert result["r2_metrics"]["plan_repair_rejected_count"] == 0
    assert result["r2_metrics"]["plan_repair_success_count"] == 1
    assert result["state"]["processed_hints"] == result["state"]["hints"]
    assert result["interface_calls"]["PLAN"] == len(repairs)


def test_abort_policy_preserves_protocol_failure():
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=1000,
                          plan_repair_mode="on_hint", max_protocol_retries=1)
    result = asyncio.run(V5Runner(NoneClient(hint=True), config=config,
                                  tokenizer=CharacterTokenizer()).run(
        run_id="abort-repair", question="Who teaches Cindy?",
        context="Alias evidence may be missing.", store=SQLiteEventStore(), plan=plan()))
    assert result["status"] == "RUNTIME_ERROR"
    assert "PLAN_REPAIR_EXHAUSTED" in result["reason_codes"]
    assert result["state"]["plan_repair_failures"] == {}


def test_unknown_repair_error_is_not_downgraded_to_optional_rejection(monkeypatch):
    from delaybind_core import subquery_runner_r2

    def broken_apply(*args, **kwargs):
        raise ValueError("SYSTEM_SENTINEL")

    monkeypatch.setattr(subquery_runner_r2, "apply_repair", broken_apply)
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=1000,
                          plan_repair_mode="on_hint", plan_repair_failure_policy="continue_valid_plan")
    result = asyncio.run(V5Runner(NoneClient(hint=True), config=config,
                                  tokenizer=CharacterTokenizer()).run(
        run_id="system-repair-error", question="Who teaches Cindy?",
        context="Alias evidence may be missing.", store=SQLiteEventStore(), plan=plan()))
    assert result["status"] == "RUNTIME_ERROR"
    assert "SYSTEM_SENTINEL" in result["reason_codes"]
    assert result["state"]["plan_repair_failures"] == {}


def test_restart_after_committed_rejection_skips_same_basis(monkeypatch):
    from delaybind_core import subquery_runner_r2

    client, store = NoneClient(hint=True), SQLiteEventStore()
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=1000,
                          plan_repair_mode="on_hint", plan_repair_failure_policy="continue_valid_plan",
                          max_protocol_retries=1)
    runner = V5Runner(client, config=config, tokenizer=CharacterTokenizer())
    kwargs = dict(run_id="rejection-restart", question="Who teaches Cindy?",
                  context="Alias evidence may be missing.", store=store, plan=plan())
    original = subquery_runner_r2.record_plan_repair_rejection
    fired = False

    def interrupt_after_record(*args, **kwargs):
        nonlocal fired
        original(*args, **kwargs)
        if not fired:
            fired = True
            raise SystemExit("injected exit after durable rejection")

    monkeypatch.setattr(subquery_runner_r2, "record_plan_repair_rejection", interrupt_after_record)
    with pytest.raises(SystemExit):
        asyncio.run(runner.run(**kwargs))
    assert len(store.latest_v52_state("rejection-restart")["plan_repair_failures"]) == 1
    before = sum(kind == "PLAN" for kind, _ in client.calls)
    result = asyncio.run(runner.run(**kwargs))
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    assert sum(kind == "PLAN" for kind, _ in client.calls) == before
    assert result["r2_metrics"]["transaction_replay_consistency"] == 1


def test_plain_chunk_configuration_requires_fact_member_inputs():
    with pytest.raises(ValueError, match="fact-only R2"):
        RunnerConfig(protocol_version="v5.2-r2", update_input_mode="plain_token_chunks")
    assert "update_input_mode" not in RunnerConfig().protocol_mapping()
    assert "plan_repair_failure_policy" not in RunnerConfig().protocol_mapping()


def test_plain_chunk_run_never_constructs_archive_and_missing_tokenizer_is_explicit(monkeypatch):
    def forbidden_archive(*args, **kwargs):
        raise AssertionError("SentenceArchive must not be constructed")

    monkeypatch.setattr("delaybind_core.subquery_runner_r2.SentenceArchive", forbidden_archive)
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=5)
    result = asyncio.run(V5Runner(NoneClient(), config=config, tokenizer=CharacterTokenizer()).run(
        run_id="no-archive", question="Who teaches Cindy?", context="abcdef",
        store=SQLiteEventStore(), plan=plan()))
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    with pytest.raises(ValueError, match="PLAIN_CHUNKS_REQUIRE_CONTEXT_AND_TOKENIZER"):
        asyncio.run(V5Runner(NoneClient(), config=config).run(
            run_id="missing-tokenizer", question="Who teaches Cindy?", context="abcdef",
            store=SQLiteEventStore(), plan=plan()))


def test_plain_chunk_full_request_budget_fails_without_shortening_body():
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=8,
                          max_input_tokens=1)
    result = asyncio.run(V5Runner(NoneClient(), config=config, tokenizer=CharacterTokenizer()).run(
        run_id="plain-budget", question="Who teaches Cindy?", context="abcdefghij",
        store=SQLiteEventStore(), plan=plan()))
    assert result["status"] == "RESOURCE_LIMIT"
    assert "UPDATE_INPUT_BUDGET" in result["reason_codes"]
    assert result["state"]["pending_chunk"]["chunk_text"] == "abcdefgh"


def test_interrupted_chunk_reuses_registered_snapshot():
    class InterruptClient(NoneClient):
        interrupt = True

        async def complete(self, *, interface, messages, **kwargs):
            if interface == "UPDATE" and self.interrupt:
                self.interrupt = False
                raise SystemExit("injected exit before UPDATE response")
            return await super().complete(interface=interface, messages=messages, **kwargs)

    client, store = InterruptClient(), SQLiteEventStore()
    cfg = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                       update_input_mode="plain_token_chunks", chunk_size=8)
    runner = V5Runner(client, config=cfg, tokenizer=CharacterTokenizer())
    kwargs = dict(run_id="chunk-interrupt", question="Who teaches Cindy?",
                  context="Document 7: First. Document 2: Second.", store=store, plan=plan())
    with pytest.raises(SystemExit):
        asyncio.run(runner.run(**kwargs))
    assert store.latest_v52_state("chunk-interrupt")["pending_chunk"]["chunk_index"] == 0
    first_snapshots = [m for m in store.list_context_manifests("chunk-interrupt")
                       if m.get("interface") == "UPDATE_SNAPSHOT"]
    assert len(first_snapshots) == 1
    result = asyncio.run(runner.run(**kwargs))
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    snapshots = [m for m in result["context_manifests"] if m.get("interface") == "UPDATE_SNAPSHOT"]
    assert snapshots[0] == first_snapshots[0]
    assert len(snapshots) == result["windows_processed"]
    assert result["state"]["pending_chunk"] is None


def test_partial_update_repair_resumes_same_scope_and_retained_fact():
    class RepairClient(NoneClient):
        interrupted = False

        async def complete(self, *, interface, messages, **kwargs):
            if interface == "UPDATE":
                self.calls.append((interface, messages))
                return "Q2 | Alice was born in Paris.\nQ99 | Alice was born in Rome."
            if interface == "UPDATE_REPAIR":
                self.calls.append((interface, messages))
                if not self.interrupted:
                    self.interrupted = True
                    raise SystemExit("injected exit before repair response")
                return "Q2 | Alice was born in Rome."
            return await super().complete(interface=interface, messages=messages, **kwargs)

    dependent_plan = EvidencePlanR2(plan_id="dependent", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Where was ?teacher born?", output="?place",
             inputs={"?teacher": "Q1"})])
    client, store = RepairClient(), SQLiteEventStore()
    cfg = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                       update_input_mode="plain_token_chunks", chunk_size=1000,
                       max_protocol_retries=1)
    runner = V5Runner(client, config=cfg, tokenizer=CharacterTokenizer())
    kwargs = dict(run_id="partial-update", question="Where was Cindy's teacher born?",
                  context="Document 1: Alice was born in Paris and Rome.",
                  store=store, plan=dependent_plan)
    with pytest.raises(SystemExit):
        asyncio.run(runner.run(**kwargs))
    saved = store.latest_v52_state("partial-update")
    assert saved["pending_chunk"] is not None
    assert saved["update_repair_progress"]["next_attempt"] == 1
    assert len(saved["facts"]) == 1
    result = asyncio.run(runner.run(**kwargs))
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    assert len(result["state"]["facts"]) == 2
    assert sum(kind == "UPDATE" for kind, _ in client.calls) == 1
    assert result["state"]["update_repair_progress"] is None
    assert result["r2_metrics"]["transaction_replay_consistency"] == 1


def test_completed_update_resume_continues_at_next_chunk(monkeypatch):
    from delaybind_core.runtime_r2 import RuntimeR2

    client, store = NoneClient(), SQLiteEventStore()
    cfg = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                       update_input_mode="plain_token_chunks", chunk_size=8)
    runner = V5Runner(client, config=cfg, tokenizer=CharacterTokenizer())
    kwargs = dict(run_id="after-update", question="Who teaches Cindy?",
                  context="Document 7: First. Document 2: Second.", store=store, plan=plan())
    original = RuntimeR2.finish_chunk
    fired = False

    def interrupt_after_finish(self):
        nonlocal fired
        original(self)
        if not fired:
            fired = True
            raise SystemExit("injected exit after chunk completion")

    monkeypatch.setattr(RuntimeR2, "finish_chunk", interrupt_after_finish)
    with pytest.raises(SystemExit):
        asyncio.run(runner.run(**kwargs))
    assert store.latest_v52_state("after-update")["pending_chunk"] is None
    result = asyncio.run(runner.run(**kwargs))
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    assert sum(kind == "UPDATE" for kind, _ in client.calls) == result["windows_processed"]
