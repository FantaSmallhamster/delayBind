"""V5.1 fact memory, per-query uses, and versioned binding transitions."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from pydantic import Field

from .archive import FutureSourceAccessError, RawArchive
from .fact_protocol import BindingProposal, FactUpdate
from .plan_validation import QUERY_VARIABLE, ensure_valid_plan
from .schema import QueryPlan, RuntimeEvent, RuntimeStatus, StrictModel, Subquery
from .storage import SQLiteEventStore
from .working_memory import BindingLink, FactNode, FactUse, QueryExecution, render_fact_memory


class SubqueryState(StrictModel):
    plan: QueryPlan
    facts: dict[str, FactNode] = Field(default_factory=dict)
    uses: dict[str, dict[str, FactUse]] = Field(default_factory=dict)
    executions: dict[str, QueryExecution] = Field(default_factory=dict)
    links: dict[str, BindingLink] = Field(default_factory=dict)
    active_fact_ids: list[str] = Field(default_factory=list)
    review_results: dict[str, str] = Field(default_factory=dict)
    hints: list[dict[str, Any]] = Field(default_factory=list)
    status: RuntimeStatus = RuntimeStatus.RUNNING
    reason_codes: list[str] = Field(default_factory=list)

    @property
    def bindings(self) -> dict[str, Any]:
        return {query.output: self.executions[query.id].result for query in self.plan.queries
                if self.executions[query.id].status == "RESOLVED"}

    def accepted_ids(self, query_id: str | None = None) -> set[str]:
        ids: set[str] = set()
        for qid, uses in self.uses.items():
            if query_id is not None and qid != query_id:
                continue
            execution = self.executions[qid]
            ids.update(fid for fid, use in uses.items() if use.status == "ACCEPTED"
                       and use.query_version == execution.version
                       and use.binding_version == execution.binding_version)
        return ids

    def query_projection(self, query_id: str) -> dict[str, Any]:
        query = next(query for query in self.plan.queries if query.id == query_id)
        execution = self.executions[query_id]
        bindings = self.bindings
        rendered = QUERY_VARIABLE.sub(
            lambda match: (str(bindings[match.group()]) if isinstance(bindings.get(match.group()), str)
                           else json.dumps(bindings[match.group()], ensure_ascii=False))
            if match.group() in bindings else match.group(), query.template,
        )
        return {
            **query.model_dump(mode="json"), **execution.model_dump(mode="json"),
            "rendered_query": rendered,
            "support_refs": sorted({ref for fid in execution.support_fact_ids
                                    for ref in self.facts[fid].source_refs}),
        }

    def memory(self, *, visible_only: bool = False) -> dict[str, Any]:
        ids = self.accepted_ids()
        if visible_only:
            ids &= set(self.active_fact_ids)
        facts = [fact.model_dump(mode="json") for fid, fact in self.facts.items() if fid in ids]
        links = [link.model_dump(mode="json") for link in self.links.values()
                 if link.downstream_fact_id in ids and set(link.upstream_fact_ids) <= ids]
        return {"facts": facts, "links": links}

    def export(self) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "queries": [self.query_projection(query.id) for query in self.plan.queries],
            "bindings": self.bindings,
            "defer_workspace": {qid: [fid for fid, use in uses.items() if use.status != "ACCEPTED"]
                                for qid, uses in self.uses.items()},
            "working_memory": self.memory(),
        }

    def apply_logged_event(self, event: RuntimeEvent) -> None:
        payload = event.payload
        if event.event_type == "FACT_STORED":
            fact = FactNode.model_validate(payload["fact"])
            self.facts[fact.fact_id] = fact
        elif event.event_type in {"FACT_COMMITTED", "FACT_DEFERRED", "FACT_PROMOTED", "FACT_USE_INVALIDATED"}:
            use = FactUse.model_validate(payload["use"])
            self.uses.setdefault(use.query_id, {})[use.fact_id] = use
        elif event.event_type == "QUERY_STATE_UPDATED":
            self.executions[payload["query_id"]] = QueryExecution.model_validate(payload["state"])
        elif event.event_type == "QUERY_PLAN_PATCHED":
            self.plan = QueryPlan.model_validate(payload["plan"])
            for query in self.plan.queries:
                self.executions.setdefault(query.id, QueryExecution())
        elif event.event_type == "BINDING_LINK_CREATED":
            link = BindingLink.model_validate(payload["link"])
            self.links[link.link_id] = link
        elif event.event_type == "BINDING_LINK_REMOVED":
            self.links.pop(payload["link_id"], None)
        elif event.event_type == "ACTIVE_VIEW_UPDATED":
            self.active_fact_ids = list(payload["fact_ids"])
        elif event.event_type == "CANDIDATE_REVIEWED":
            self.review_results[payload["key"]] = payload["verdict"]
        elif event.event_type == "PLAN_HINT_RECORDED":
            self.hints.append(dict(payload))
        elif event.event_type == "RUN_STATUS":
            self.status = RuntimeStatus(payload["status"])
            self.reason_codes = list(payload["reason_codes"])


def initial_subquery_state(plan: QueryPlan) -> SubqueryState:
    return SubqueryState(plan=plan, executions={query.id: QueryExecution() for query in plan.queries})


class SubqueryRuntime:
    def __init__(self, *, run_id: str, plan: QueryPlan, archive: RawArchive,
                 store: SQLiteEventStore, defer_unbound: bool = True):
        ensure_valid_plan(plan)
        self.run_id, self.archive, self.store = run_id, archive, store
        self.defer_unbound = defer_unbound
        self.state = initial_subquery_state(plan)
        self._activations: list[str] = []
        self.emit("PLAN_CREATED", {"plan": plan.model_dump(mode="json"), "plan_format": "subqueries",
                                   "protocol_version": "v5.1"})
        self._activate_ready(window_index=0, from_binding=False)

    def emit(self, event_type: str, payload: dict[str, Any], *, source_ref: str | None = None) -> RuntimeEvent:
        event = self.store.append_runtime_event(RuntimeEvent(
            run_id=self.run_id, event_id=str(uuid4()), event_type=event_type,
            payload=payload, source_ref=source_ref,
        ))
        self.state.apply_logged_event(event)
        return event

    def _set_query(self, query_id: str, **changes: Any) -> None:
        execution = self.state.executions[query_id].model_copy(update=changes)
        self.emit("QUERY_STATE_UPDATED", {"query_id": query_id, "state": execution.model_dump(mode="json")})

    def dependency_ids(self, query_id: str) -> set[str]:
        queries = {query.id: query for query in self.state.plan.queries}
        found: set[str] = set()
        remaining = list(queries[query_id].depends_on)
        while remaining:
            dependency = remaining.pop()
            if dependency not in found:
                found.add(dependency)
                remaining.extend(queries[dependency].depends_on)
        return found

    def descendants(self, query_id: str) -> set[str]:
        return {query.id for query in self.state.plan.queries if query_id in self.dependency_ids(query.id)}

    def _activate_ready(self, *, window_index: int, from_binding: bool) -> None:
        for query in self.state.plan.queries:
            execution = self.state.executions[query.id]
            if execution.status != "DORMANT":
                continue
            if all(self.state.executions[dep].status == "RESOLVED" for dep in query.depends_on):
                pending = from_binding and bool(query.depends_on)
                self._set_query(query.id, status="ACTIVE", activation_window=window_index,
                                review_pending=pending, binding_version=execution.binding_version + 1)
                self.emit("QUERY_ACTIVATED", {"query_id": query.id, "window_index": window_index,
                                               "trigger": "BINDING" if pending else "INITIAL_OR_PATCH"})
                if pending:
                    self._activations.append(query.id)

    def drain_activations(self) -> list[str]:
        query_ids, self._activations = list(dict.fromkeys(self._activations)), []
        return query_ids

    def finish_review(self, query_id: str) -> None:
        self._set_query(query_id, review_pending=False)

    def ingest(self, update: FactUpdate, *, window_index: int, allowed_refs: set[str]) -> list[str]:
        """Register all window facts before any binding or callback is applied."""
        committed: list[str] = []
        for event in update.facts:
            if event.query_id not in self.state.executions:
                self.emit("FACT_SKIPPED", {"reason": "UNKNOWN_QUERY", "event": event.model_dump(mode="json")})
                continue
            try:
                valid = set(event.source_refs) <= allowed_refs and all(
                    not self.archive.entry(ref).context_only for ref in event.source_refs
                )
            except FutureSourceAccessError:
                valid = False
            if not valid:
                self.emit("FACT_SKIPPED", {"reason": "INVALID_OR_UNREAD_SOURCE", "event": event.model_dump(mode="json")})
                continue
            fact = FactNode.create(event.text, event.source_refs, window_index)
            if fact.fact_id not in self.state.facts:
                self.emit("FACT_STORED", {"fact": fact.model_dump(mode="json")}, source_ref=fact.source_refs[0])
            execution = self.state.executions[event.query_id]
            defer = event.relevance == "DORMANT" or execution.status == "DORMANT"
            if defer and not self.defer_unbound:
                self.emit("FACT_SKIPPED", {"reason": "DEFER_DISABLED", "fact_id": fact.fact_id})
                continue
            prior = self.state.uses.get(event.query_id, {}).get(fact.fact_id)
            if prior and prior.query_version == execution.version and prior.binding_version == execution.binding_version:
                if prior.status == "ACCEPTED" or (defer and prior.status == "CANDIDATE"):
                    continue
            use = FactUse(fact_id=fact.fact_id, query_id=event.query_id,
                          query_version=execution.version, binding_version=execution.binding_version,
                          observed_window=prior.observed_window if prior else window_index,
                          status="CANDIDATE" if defer else "ACCEPTED", acceptance=None if defer else "COMMIT")
            self.emit("FACT_DEFERRED" if defer else "FACT_COMMITTED", {"use": use.model_dump(mode="json"),
                      "observed_relevance": event.relevance}, source_ref=fact.source_refs[0])
            if not defer:
                committed.append(fact.fact_id)
                self._connect(use)
        for hint in update.hints:
            if set(hint.source_refs) <= allowed_refs and all(self.archive.contains(ref) for ref in hint.source_refs):
                fact = FactNode.create(hint.text, hint.source_refs, window_index)
                if fact.fact_id not in self.state.facts:
                    self.emit("FACT_STORED", {"fact": fact.model_dump(mode="json")})
                payload = {"fact_id": fact.fact_id, **hint.model_dump(mode="json")}
                if payload not in self.state.hints:
                    self.emit("PLAN_HINT_RECORDED", payload)
        return committed

    def review_key(self, query_id: str, fact_id: str) -> str:
        execution = self.state.executions[query_id]
        return f"{query_id}:{execution.version}:{execution.binding_version}:{fact_id}"

    def candidate_facts(self, query_id: str) -> list[FactNode]:
        return [self.state.facts[fid] for fid, use in self.state.uses.get(query_id, {}).items()
                if use.status != "ACCEPTED" and self.review_key(query_id, fid) not in self.state.review_results]

    def apply_reviews(self, query_id: str, judgments: dict[str, str], *, selected_ids: set[str]) -> list[str]:
        execution = self.state.executions[query_id]
        if execution.status != "ACTIVE" or not execution.review_pending:
            raise ValueError("VERIFY is allowed only during a binding-triggered deferred review")
        candidates = {fact.fact_id for fact in self.candidate_facts(query_id)}
        if set(judgments) != selected_ids or not selected_ids <= candidates:
            raise ValueError("VERIFY must judge exactly the selected deferred candidates")
        promoted: list[str] = []
        conflicts = list(execution.conflicts)
        for fact_id, verdict in judgments.items():
            if verdict not in {"MATCH", "MISMATCH", "UNCERTAIN", "CONFLICT"}:
                raise ValueError(f"invalid verdict {verdict}")
            self.emit("CANDIDATE_REVIEWED", {"key": self.review_key(query_id, fact_id),
                      "query_id": query_id, "fact_id": fact_id, "verdict": verdict})
            if verdict == "CONFLICT":
                conflicts.append(fact_id)
            if verdict != "MATCH":
                continue
            fact = self.state.facts[fact_id]
            prior = self.state.uses[query_id][fact_id]
            use = FactUse(fact_id=fact_id, query_id=query_id, query_version=execution.version,
                          binding_version=execution.binding_version, observed_window=prior.observed_window,
                          status="ACCEPTED", acceptance="PROMOTE")
            self.emit("FACT_PROMOTED", {"use": use.model_dump(mode="json"),
                      "origin": prior.status,
                      "cross_window": prior.status == "CANDIDATE" and prior.observed_window < execution.activation_window}, source_ref=fact.source_refs[0])
            self._connect(use)
            promoted.append(fact_id)
        if conflicts != execution.conflicts:
            self._set_query(query_id, conflicts=conflicts)
        return promoted

    def _connect(self, use: FactUse) -> None:
        query = next(query for query in self.state.plan.queries if query.id == use.query_id)
        for dependency in query.depends_on:
            upstream = next(query for query in self.state.plan.queries if query.id == dependency)
            execution = self.state.executions[dependency]
            if not execution.support_fact_ids:
                continue
            data = {
                "upstream_fact_ids": sorted(execution.support_fact_ids), "downstream_fact_id": use.fact_id,
                "upstream_query_id": dependency, "query_id": use.query_id,
                "variable": upstream.output, "value": execution.result,
                "query_version": use.query_version, "binding_version": use.binding_version,
            }
            identity = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            if identity not in self.state.links:
                link = BindingLink(link_id=identity, **data)
                self.emit("BINDING_LINK_CREATED", {"link": link.model_dump(mode="json")})

    def resolve_support(self, query_id: str, refs: list[str]) -> list[str]:
        allowed = self.state.accepted_ids(query_id)
        for dependency in self.dependency_ids(query_id):
            allowed.update(self.state.executions[dependency].support_fact_ids)
        support: set[str] = set()
        for ref in refs:
            if ref in allowed:
                support.add(ref)
                continue
            matches = {fid for fid in allowed if ref in self.state.facts[fid].source_refs}
            if not matches:
                raise ValueError(f"binding support is not an accepted fact: {ref}")
            support.update(matches)
        if not support:
            raise ValueError("binding requires accepted supporting facts")
        return sorted(support)

    def apply_binding(self, proposal: BindingProposal, *, window_index: int, eof: bool = False) -> bool:
        query_id = proposal.query_id
        if query_id not in self.state.executions:
            raise ValueError(f"unknown query {query_id}")
        execution = self.state.executions[query_id]
        query = next(query for query in self.state.plan.queries if query.id == query_id)
        if execution.status == "DORMANT" or execution.review_pending or execution.conflicts:
            raise ValueError("binding requires ready inputs and no pending review or conflict")
        support = self.resolve_support(query_id, proposal.support_refs)
        # The declared input bindings are part of the proof, even when the
        # high-level BIND line cites only the newly found answer fact.
        support = sorted(set(support) | {fid for dependency in query.depends_on
                         for fid in self.state.executions[dependency].support_fact_ids})
        if query.requires_complete_set and not eof:
            self.emit("BINDING_HELD", {"query_id": query_id, "reason": "COLLECTION_SCOPE_OPEN"})
            return False
        if execution.status == "RESOLVED" and execution.result == proposal.value and execution.support_fact_ids == support:
            return False
        if execution.status == "RESOLVED":
            self.invalidate(self.descendants(query_id), reason="UPSTREAM_BINDING_CHANGED", window_index=window_index)
            for fact_id in set(execution.support_fact_ids) - set(support):
                use = self.state.uses.get(query_id, {}).get(fact_id)
                if use and use.status == "ACCEPTED":
                    self.emit("FACT_USE_INVALIDATED", {
                        "use": use.model_copy(update={"status": "INVALIDATED", "acceptance": None}).model_dump(mode="json"),
                        "reason": "BINDING_SUPPORT_REPLACED",
                    })
        self._set_query(query_id, result=proposal.value, support_fact_ids=support, status="RESOLVED",
                        resolved_version=execution.resolved_version + 1)
        self.emit("QUERY_BOUND", {"query_id": query_id, "variable": query.output, "value": proposal.value,
                  "supporting_fact_ids": support, "window_index": window_index,
                  "resolved_version": execution.resolved_version + 1})
        self._activate_ready(window_index=window_index, from_binding=True)
        return True

    def invalidate(self, query_ids: set[str], *, reason: str, window_index: int) -> None:
        for query_id in query_ids:
            execution = self.state.executions[query_id]
            for use in list(self.state.uses.get(query_id, {}).values()):
                if use.status == "ACCEPTED":
                    invalid = use.model_copy(update={"status": "INVALIDATED", "acceptance": None})
                    self.emit("FACT_USE_INVALIDATED", {"use": invalid.model_dump(mode="json"), "reason": reason})
            self._set_query(query_id, status="DORMANT", result=None, support_fact_ids=[], review_pending=False,
                            binding_version=execution.binding_version + 1, conflicts=[])
        for link_id, link in list(self.state.links.items()):
            if link.query_id in query_ids or link.upstream_query_id in query_ids:
                self.emit("BINDING_LINK_REMOVED", {"link_id": link_id, "reason": reason})
        self._activations = [qid for qid in self._activations if qid not in query_ids]

    def patch_queries(self, patches: dict[str, dict[str, Any]], *, window_index: int) -> None:
        by_id = {query.id: query.model_dump() for query in self.state.plan.queries}
        affected: set[str] = set()
        for query_id, patch in patches.items():
            if query_id in by_id:
                affected |= {query_id} | self.descendants(query_id)
            if "id" in patch and patch["id"] != query_id:
                raise ValueError("a patch cannot rename a query id")
            by_id[query_id] = {**by_id.get(query_id, {}), **patch, "id": query_id}
        plan = self.state.plan.model_copy(update={"queries": [Subquery.model_validate(value) for value in by_id.values()]})
        ensure_valid_plan(plan)
        self.invalidate(affected, reason="PLAN_PATCHED", window_index=window_index)
        self.emit("QUERY_PLAN_PATCHED", {"plan": plan.model_dump(mode="json")})
        for query_id in patches:
            self._set_query(query_id, version=self.state.executions[query_id].version + 1)
        # A patch alone must not introduce an extra VERIFY trigger.
        self._activate_ready(window_index=window_index, from_binding=False)

    def set_visible(self, fact_ids: list[str] | None = None) -> None:
        accepted = self.state.accepted_ids()
        requested = accepted if fact_ids is None else set(fact_ids)
        if not requested <= accepted:
            raise ValueError("working memory can show only currently accepted facts")
        pinned = {fid for execution in self.state.executions.values() for fid in execution.support_fact_ids}
        ordered = list(self.state.facts) if fact_ids is None else fact_ids
        visible = list(dict.fromkeys([fid for fid in ordered if fid in requested] + sorted(pinned)))
        self.emit("ACTIVE_VIEW_UPDATED", {"fact_ids": visible})

    def retract(self, query_id: str, fact_ids: list[str], *, window_index: int) -> None:
        if not set(fact_ids) <= self.state.accepted_ids(query_id):
            raise ValueError("RETRACT requires facts accepted for this query")
        for fact_id in fact_ids:
            use = self.state.uses[query_id][fact_id].model_copy(update={"status": "INVALIDATED", "acceptance": None})
            self.emit("FACT_USE_INVALIDATED", {"use": use.model_dump(mode="json"), "reason": "HIGH_AGENT_RETRACTED"})
        execution = self.state.executions[query_id]
        if set(fact_ids) & set(execution.support_fact_ids):
            self.invalidate(self.descendants(query_id), reason="UPSTREAM_SUPPORT_RETRACTED", window_index=window_index)
            self._set_query(query_id, result=None, support_fact_ids=[], status="ACTIVE", conflicts=[])
        for link_id, link in list(self.state.links.items()):
            if link.downstream_fact_id in fact_ids and link.query_id == query_id:
                self.emit("BINDING_LINK_REMOVED", {"link_id": link_id, "reason": "FACT_RETRACTED"})

    def route_candidates(self, query_id: str, fact_ids: list[str], *, window_index: int = 0) -> None:
        if query_id not in self.state.executions or not set(fact_ids) <= self.state.facts.keys():
            raise ValueError("ROUTE requires an existing query and saved facts")
        execution = self.state.executions[query_id]
        for fact_id in fact_ids:
            if fact_id in self.state.accepted_ids(query_id):
                continue
            use = FactUse(fact_id=fact_id, query_id=query_id, query_version=execution.version,
                          binding_version=execution.binding_version, observed_window=window_index, status="CANDIDATE")
            self.emit("FACT_DEFERRED", {"use": use.model_dump(mode="json"), "reason": "PLAN_REROUTE"})

    def set_status(self, status: RuntimeStatus, reasons: list[str]) -> None:
        self.emit("RUN_STATUS", {"status": status.value, "reason_codes": reasons})

    def evidence_pack(self) -> dict[str, Any]:
        return self.state.memory()

    def memory_text(self, *, visible_only: bool = True) -> str:
        memory = self.state.memory(visible_only=visible_only)
        return render_fact_memory([FactNode.model_validate(fact) for fact in memory["facts"]],
                                  [BindingLink.model_validate(link) for link in memory["links"]])


def replay_subqueries(events: list[RuntimeEvent], plan: QueryPlan) -> SubqueryState:
    state = initial_subquery_state(plan)
    for event in events:
        state.apply_logged_event(event)
    return state
