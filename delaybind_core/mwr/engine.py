"""Explicit forward-only MWR engine. Legacy retrieval is never imported here."""
from uuid import uuid4
from . import SCHEMA_VERSION
from .common import digest, dump, run_contract, ResourceLimit, ProtocolError
from .models import InferenceInput, LiveState, EvidenceRecord, Update, Plan
from .source_access import SourceAccess
from .budget import CallBudget
from .evidence import stage_proposals
from .demands import validate_plan, reading_messages
from .retention import retain, tokens
from .binding import process_events
from .packing import pack_answer
from .answering import solve_answer, AnswerContract
from ..schema import RuntimeEvent


async def run_mwr(*, client, config, run_id, sample, store, manifest=None, plan=None,
                  reader_client=None, tokenizer=None):
    inp = InferenceInput.from_sample(sample)
    contract = run_contract(config, inp.question, inp.model_dump(), client, reader_client,
                            tokenizer, plan.model_dump() if plan else None)
    namespace = digest(contract)
    def log(kind, payload):
        store.append_runtime_event(RuntimeEvent(run_id=run_id, event_id=str(uuid4()),
            event_type=kind, payload={"schema_version": SCHEMA_VERSION, **payload}))
    access = SourceAccess(inp, tokenizer, manifest=manifest, neighbor_anchors=config.mwr.neighbor_anchors, log=log)
    state = LiveState()
    if tokens(state,tokenizer) > config.mwr.max_live_state_tokens:
        raise ResourceLimit("empty state metadata exceeds live-state budget")
    calls = CallBudget(client, reader_client, tokenizer, config, run_id, namespace, log)
    meter = store.mwr_meter(run_id, namespace)
    calls.calls = meter["logical_calls"]
    access.lookup_tokens = meter["reread_tokens"]
    def persist_meter():
        store.save_mwr_meter(run_id, namespace, calls.calls, access.lookup_tokens)
    calls.on_meter = access.on_meter = persist_meter
    saved = store.latest_snapshot(run_id)
    if saved:
        payload = saved["payload"]
        if payload.get("contract_hash") != namespace:
            raise ProtocolError("run contract changed; use a new run_id")
        state = LiveState.model_validate(payload["state"])
        access.restore(payload["source"])
        calls.calls = max(payload["calls"], meter["logical_calls"])
        access.lookup_tokens = max(access.lookup_tokens, meter["reread_tokens"])
        if payload.get("result"):
            return payload["result"]
    def checkpoint(result=None):
        store.save_snapshot(run_id, "mwr-current", state.version,
             {"contract_hash": namespace, "contract": contract, "state": state.model_dump(),
              "source": access.checkpoint(), "calls": calls.calls, "observed_ids": sorted(observed_ids), "result": result})
    observed_ids = set(saved["payload"].get("observed_ids", [])) if saved else set()
    if not saved:
        if plan is not None:
            if not hasattr(plan, "queries"):
                raise ProtocolError("MWR requires a natural-language subquery plan")
            state.plan = validate_plan(Plan(queries=[{k:v for k,v in q.model_dump().items() if k != "required"} for q in plan.queries]))
        elif config.engine == "delaybind_mwr":
            state.plan = validate_plan(await calls.invoke("PLAN", [{"role":"user","content":
                "Create a compact DAG of natural-language evidence demands using only the question. "
                "Do not fill unknown entities. Every output must be a unique ?variable such as ?person. "
                "Use literal known names in templates; unknown template inputs must be ?variables produced by listed depends_on query IDs. "
                "Do not use {braces} or $variables. Break multi-hop questions into the needed source relations. "
                "For example, birthplace of a film director needs a director query followed by a birthplace query depending on it. "
                "Include compact relation_cues. requires_complete_set is false for ordinary single-value questions. "
                "Empty queries are allowed when no decomposition is useful. Question: " + inp.question}], Plan, validator=validate_plan))
    if not saved:
        log("PLAN_CREATED", {"plan":state.plan.model_dump(),"state_version":state.version})
    try:
        state = retain(state, tokenizer, config.mwr, log)
    except ResourceLimit:
        # Refuse an over-budget plan rather than checkpointing an illegal state.
        state = LiveState(status="RESOURCE_LIMIT")
        log("BUDGET_BLOCKED", {"reason":"initial_plan_budget"})
    access.commit(state)
    checkpoint()
    while not access.exhausted and state.status != "RESOURCE_LIMIT":
        if access.windows >= config.max_windows:
            state.status = "RESOURCE_LIMIT"
            break
        rows = access.next_window(config.chunk_size)
        try:
            proposal = await calls.invoke("UPDATE", reading_messages(inp.question, rows, state, config.mwr), Update, role="LOW")
            staged = stage_proposals(proposal, state, access, tokenizer, config.mwr, log, observed_ids)
            # Temporary union is bounded by live + staging; dependency events run
            # before retention so a same-window bridge is not thrown away first.
            working = state.model_copy(deep=True)
            working.evidence.update(staged)
            working.plan_notes = []
            for note in proposal.plan_notes:
                staged_payload={"evidence":{k:v.model_dump() for k,v in staged.items()},
                                "plan_notes":[*working.plan_notes,note]}
                if len(tokenizer.encode(dump(staged_payload))) <= config.mwr.max_staging_tokens:
                    working.plan_notes.append(note)
                else:
                    log("BUDGET_BLOCKED", {"reason":"staging_plan_note_tokens"})
            access.authorize(working, staged)
            await process_events(working, access, calls, config, tokenizer, log, staged=staged)
            working.version += 1
            state = retain(working, tokenizer, config.mwr, log)
        except ResourceLimit as exc:
            state.status = "RESOURCE_LIMIT"
            log("BUDGET_BLOCKED", {"reason":str(exc),"state_version":state.version})
        proposal, staged, working, rows = None, {}, None, None
        access.commit(state)
        assert tokens(state,tokenizer) <= config.mwr.max_live_state_tokens
        log("WINDOW_COMMITTED", {"state_version":state.version,"window":access.windows,
              "cursor":access.position(),"live_state_tokens":tokens(state,tokenizer),
              "evidence_ids":sorted(state.evidence),"staging_tokens":0,
              "reachable_spans":[{k:v for k,v in r.items() if k not in {"text"}} for r in access.preview_for_evidence(list(state.evidence), state_version=state.version)]})
        checkpoint()
    if state.status != "RESOURCE_LIMIT":
        try:
            await process_events(state, access, calls, config, tokenizer, log, eof=True)
            state = retain(state, tokenizer, config.mwr, log)
        except ResourceLimit as exc:
            state.status = "RESOURCE_LIMIT"
            log("BUDGET_BLOCKED", {"reason":str(exc)})
    access.commit(state)
    bundle = {"raw_sources":[],"supported_relations":[],"unresolved_demands":[],"conflicts":[]}
    from .models import Answer
    answer = Answer(status="INSUFFICIENT")
    try:
        bundle = pack_answer(inp.question,state,access,calls,config,tokenizer,log)
        answer = await solve_answer(inp.question, bundle, AnswerContract(), calls, config.mwr)
    except ResourceLimit as exc:
        state.status = "RESOURCE_LIMIT"
        log("BUDGET_BLOCKED", {"reason":str(exc)})
    if state.status != "RESOURCE_LIMIT":
        state.status = answer.status
    result = {"run_id": run_id, "engine": config.engine, "schema_version": SCHEMA_VERSION,
              "contract_hash": namespace, "answer": answer.model_dump(), "state": state.model_dump(),
              "evidence_pack": {**bundle, "answer_value": answer.answer},
              "windows_processed": access.windows, "logical_model_calls": calls.calls,
              "live_state_tokens":tokens(state,tokenizer),
              "bookkeeping_bytes":len(dump(access.checkpoint()).encode())+len(dump(sorted(observed_ids)).encode()),
              "reread_tokens": access.lookup_tokens, "status": state.status, **access.storage_metrics()}
    checkpoint(result)
    return result
