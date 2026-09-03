"""Deterministic delayed-binding runtime.

The runtime treats model actions as proposals.  It owns disposition, binding,
deferred lookup, claim promotion and conflict bookkeeping.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .archive import FutureSourceAccessError, RawArchive
from .operators import OperatorError, execute_operator
from .schema import (
    Action,
    Claim,
    Disposition,
    EvidenceAssertion,
    EvidenceKind,
    EvidencePack,
    Pattern,
    QueryPlan,
    RuntimeEvent,
    RuntimeStatus,
    TripleEvent,
    VerifyResult,
    VerifyStatus,
)
from .storage import SQLiteEventStore


def _norm(value: Any) -> str:
    return " ".join(str(value).casefold().split())


def _is_var(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("?")


@dataclass
class RuntimeState:
    plan: QueryPlan
    bindings: dict[str, Any] = field(default_factory=dict)
    deferred: dict[str, Claim] = field(default_factory=dict)
    pending: dict[str, Claim] = field(default_factory=dict)
    verified: dict[str, Claim] = field(default_factory=dict)
    rejected: dict[str, Claim] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    status: RuntimeStatus = RuntimeStatus.RUNNING
    reason_codes: list[str] = field(default_factory=list)
    operator_trace: list[dict[str, Any]] = field(default_factory=list)

    def export(self) -> dict[str, Any]:
        return {
            "plan": self.plan.model_dump(mode="json"),
            "bindings": self.bindings,
            "deferred": {k: v.model_dump(mode="json") for k, v in self.deferred.items()},
            "pending": {k: v.model_dump(mode="json") for k, v in self.pending.items()},
            "verified": {k: v.model_dump(mode="json") for k, v in self.verified.items()},
            "rejected": {k: v.model_dump(mode="json") for k, v in self.rejected.items()},
            "conflicts": self.conflicts,
            "status": self.status.value,
            "reason_codes": self.reason_codes,
            "operator_trace": self.operator_trace,
        }

    def graph_projection(self, *, max_claims: int | None = None) -> dict[str, Any]:
        """Return the compact verified graph view intended for UPDATE prompts."""
        claims = list(self.verified.values())
        if max_claims is not None:
            claims = [] if max_claims <= 0 else claims[-max_claims:]
        return {
            "bindings": dict(self.bindings),
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "subject": claim.subject,
                    "relation": claim.relation,
                    "object": claim.object,
                    "qualifiers": claim.qualifiers,
                    "polarity": claim.polarity.value,
                    "modality": claim.modality.value,
                    "matched_pattern_id": claim.matched_pattern_id,
                }
                for claim in claims
            ],
        }


@dataclass
class RuntimeResult:
    claim: Claim | None = None
    applied_action: Action | None = None
    pattern_id: str | None = None
    newly_bound: dict[str, Any] = field(default_factory=dict)
    deferred_matches: list[Claim] = field(default_factory=list)
    verification_candidates: list[Claim] = field(default_factory=list)
    events: list[RuntimeEvent] = field(default_factory=list)


class EvidenceRuntime:
    def __init__(
        self,
        *,
        run_id: str,
        plan: QueryPlan,
        archive: RawArchive,
        store: SQLiteEventStore,
        verify_committed: bool = False,
        defer_unbound: bool = True,
    ):
        self.run_id = run_id
        self.archive = archive
        self.store = store
        self.verify_committed = verify_committed
        self.defer_unbound = defer_unbound
        self._verification_matches: list[Claim] = []
        self._operators_executed = False
        self.state = RuntimeState(plan=plan)
        self._emit(
            "PLAN_CREATED",
            {"plan": plan.model_dump(mode="json")},
        )

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        source_ref: str | None = None,
        stream_position: int | None = None,
        caused_by_event_seq: int | None = None,
        event_id: str | None = None,
    ) -> RuntimeEvent:
        raw = json.dumps(
            {
                "run_id": self.run_id,
                "event_type": event_type,
                "payload": payload,
                "source_ref": source_ref,
                "stream_position": stream_position,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        event = RuntimeEvent(
            event_id=event_id or hashlib.sha256(raw.encode()).hexdigest(),
            run_id=self.run_id,
            event_type=event_type,
            payload=payload,
            source_ref=source_ref,
            stream_position=stream_position,
            caused_by_event_seq=caused_by_event_seq,
            transaction_id=str(uuid4()),
        )
        return self.store.append_runtime_event(event)

    def _patterns_for(self, event: TripleEvent) -> list[Pattern]:
        candidates = []
        for pattern in self.state.plan.patterns:
            relation_key = event.matched_family or event.concrete_relation
            if (
                _norm(relation_key) == _norm(pattern.relation)
                or (pattern.relation_family and _norm(relation_key) == _norm(pattern.relation_family))
            ):
                candidates.append(pattern)
        if event.pattern_hint:
            hinted = [p for p in candidates if p.id == event.pattern_hint]
            if hinted:
                return hinted
        return candidates

    def _pattern_matches_known(self, pattern: Pattern, event: TripleEvent) -> bool:
        subject = self.state.bindings.get(pattern.subject, pattern.subject)
        obj = self.state.bindings.get(pattern.object, pattern.object)
        if not _is_var(subject) and _norm(subject) != _norm(event.subject):
            return False
        if not _is_var(obj) and _norm(obj) != _norm(event.object):
            return False
        return True

    def _pattern_is_groundable(self, pattern: Pattern) -> bool:
        """A pattern is grounded if its non-variable side is known.

        This is the key delayed-binding distinction: ``(?person, birth_year,
        ?year)`` is related to the plan but is not yet assigned to a target
        person, so it must be deferred.
        """
        known_subject = not _is_var(pattern.subject) or pattern.subject in self.state.bindings
        known_object = not _is_var(pattern.object) or pattern.object in self.state.bindings
        return known_subject or known_object

    def _claim_from_event(self, event: TripleEvent, pattern: Pattern | None = None) -> Claim:
        claim_id = "claim_" + hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:16]
        return Claim(
            claim_id=claim_id,
            subject=event.subject,
            relation=event.matched_family or event.concrete_relation,
            object=event.object,
            qualifiers=event.qualifiers,
            polarity=event.polarity,
            modality=event.modality,
            evidence_kind=EvidenceKind.RAW,
            disposition=Disposition.DEFERRED,
            evidence_assertions=[EvidenceAssertion(source_ref=event.source_ref)],
            matched_pattern_id=pattern.id if pattern is not None else None,
        )

    def _bind_from(self, pattern: Pattern, event: TripleEvent) -> dict[str, Any]:
        new_bindings: dict[str, Any] = {}
        for slot, actual in ((pattern.subject, event.subject), (pattern.object, event.object)):
            if _is_var(slot) and slot not in self.state.bindings:
                self.state.bindings[slot] = actual
                new_bindings[slot] = actual
        return new_bindings

    def _bind_from_claim(self, claim: Claim) -> dict[str, Any]:
        """Bind variables from a promoted deferred claim and cascade lookups."""
        new_bindings: dict[str, Any] = {}
        for pattern in self.state.plan.patterns:
            if _norm(pattern.relation_key) != _norm(claim.relation):
                continue
            subject = self.state.bindings.get(pattern.subject, pattern.subject)
            obj = self.state.bindings.get(pattern.object, pattern.object)
            if not _is_var(subject) and _norm(subject) != _norm(claim.subject):
                continue
            if not _is_var(obj) and _norm(obj) != _norm(claim.object):
                continue
            for slot, actual in (
                (pattern.subject, claim.subject),
                (pattern.object, claim.object),
            ):
                if _is_var(slot) and slot not in self.state.bindings:
                    self.state.bindings[slot] = actual
                    new_bindings[slot] = actual
        return new_bindings

    def apply_event(self, event: TripleEvent) -> RuntimeResult:
        source_entry = self.archive.entry(event.source_ref)
        if source_entry.context_only:
            self._emit(
                "SOURCE_ACCESS_REJECTED",
                {"reason": "CONTEXT_ONLY_SOURCE", "event": event.model_dump(mode="json")},
                source_ref=event.source_ref,
                stream_position=source_entry.stream_position,
            )
            raise ValueError(f"source_ref {event.source_ref!r} is context_only")
        if event.source_order != source_entry.stream_position:
            self._emit(
                "EVENT_SOURCE_ORDER_MISMATCH",
                {
                    "model_source_order": event.source_order,
                    "manifest_source_order": source_entry.stream_position,
                },
                source_ref=event.source_ref,
                stream_position=source_entry.stream_position,
            )
            event = event.model_copy(update={"source_order": source_entry.stream_position})

        if not self.archive.contains(event.source_ref):
            self._emit(
                "SOURCE_ACCESS_REJECTED",
                {"reason": "FUTURE_SOURCE", "event": event.model_dump(mode="json")},
                source_ref=event.source_ref,
                stream_position=event.source_order,
            )
            raise FutureSourceAccessError(
                f"source_ref {event.source_ref!r} is not in the read prefix for run {self.run_id}"
            )

        previous = self.store.find_runtime_event(self.run_id, f"proposal:{event.event_id}")
        if previous is not None:
            claim_id = "claim_" + hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:16]
            claim = (
                self.state.deferred.get(claim_id)
                or self.state.verified.get(claim_id)
                or self.state.rejected.get(claim_id)
            )
            return RuntimeResult(
                claim=claim,
                applied_action=self._action_for_claim(claim),
                events=self.store.list_runtime_events(self.run_id),
            )

        self._emit(
            "TRIPLE_EVENT_PROPOSED",
            {"event": event.model_dump(mode="json")},
            source_ref=event.source_ref,
            stream_position=event.source_order,
            event_id=f"proposal:{event.event_id}",
        )
        patterns = [p for p in self._patterns_for(event) if self._pattern_matches_known(p, event)]
        pattern = patterns[0] if patterns else None
        claim = self._claim_from_event(event, pattern)
        if pattern is None:
            claim.disposition = Disposition.SKIPPED
            self._emit("CLAIM_SKIPPED", {"claim": claim.model_dump(mode="json")}, source_ref=event.source_ref)
            return RuntimeResult(claim=claim, applied_action=Action.SKIP, events=self.store.list_runtime_events(self.run_id))

        if self._pattern_is_groundable(pattern):
            claim.disposition = Disposition.COMMITTED
            applied = Action.COMMIT
            self._emit(
                "CLAIM_COMMITTED",
                {
                    "claim": claim.model_dump(mode="json"),
                    "pattern_id": pattern.id,
                    "verification_required": self.verify_committed,
                },
                source_ref=event.source_ref,
            )
            if self.verify_committed:
                self.state.pending[claim.claim_id] = claim
                return RuntimeResult(
                    claim=claim,
                    applied_action=applied,
                    pattern_id=pattern.id,
                    verification_candidates=[claim],
                    events=self.store.list_runtime_events(self.run_id),
                )
            newly_bound = self._bind_from(pattern, event)
            self.state.verified[claim.claim_id] = claim
            for variable, value in newly_bound.items():
                self._emit("ENTITY_BOUND", {"variable": variable, "value": value})
            matches = self._lookup_deferred(newly_bound)
            return RuntimeResult(
                claim=claim,
                applied_action=applied,
                pattern_id=pattern.id,
                newly_bound=newly_bound,
                deferred_matches=matches,
                events=self.store.list_runtime_events(self.run_id),
            )

        claim.disposition = Disposition.DEFERRED
        if not self.defer_unbound:
            claim.disposition = Disposition.SKIPPED
            self._emit(
                "CLAIM_SKIPPED",
                {
                    "claim": claim.model_dump(mode="json"),
                    "pattern_id": pattern.id,
                    "reason": "DEFER_DISABLED",
                },
                source_ref=event.source_ref,
            )
            return RuntimeResult(
                claim=claim,
                applied_action=Action.SKIP,
                pattern_id=pattern.id,
                events=self.store.list_runtime_events(self.run_id),
            )
        applied = Action.DEFER
        self.state.deferred[claim.claim_id] = claim
        self._emit("CLAIM_DEFERRED", {"claim": claim.model_dump(mode="json"), "pattern_id": pattern.id}, source_ref=event.source_ref)
        return RuntimeResult(
            claim=claim,
            applied_action=applied,
            pattern_id=pattern.id,
            events=self.store.list_runtime_events(self.run_id),
        )

    def apply_events(self, events: list[TripleEvent]) -> list[RuntimeResult]:
        """Apply a window's events in manifest/source order.

        The model is instructed to emit this order, but Runtime reorders using
        the source positions supplied by the manifest-derived event metadata.
        """
        ordered = sorted(
            events,
            key=lambda item: (
                self.archive.entry(item.source_ref).stream_position,
                item.span_hint or "",
                item.event_id,
            ),
        )
        if [event.event_id for event in ordered] != [event.event_id for event in events]:
            self._emit(
                "EVENTS_REORDERED",
                {
                    "model_order": [event.event_id for event in events],
                    "applied_order": [event.event_id for event in ordered],
                },
            )
        return [self.apply_event(event) for event in ordered]

    @staticmethod
    def _action_for_claim(claim: Claim | None) -> Action | None:
        if claim is None:
            return None
        return {
            Disposition.DEFERRED: Action.DEFER,
            Disposition.COMMITTED: Action.COMMIT,
            Disposition.PROMOTED: Action.COMMIT,
            Disposition.SKIPPED: Action.SKIP,
            Disposition.REJECTED: Action.SKIP,
        }[claim.disposition]

    def _lookup_deferred(self, new_bindings: dict[str, Any]) -> list[Claim]:
        if not new_bindings:
            return []
        grounded_requirements: set[tuple[str, str, str]] = set()
        for pattern in self.state.plan.patterns:
            relation = _norm(pattern.relation_key)
            if _is_var(pattern.subject) and pattern.subject in new_bindings:
                grounded_requirements.add(("subject", _norm(new_bindings[pattern.subject]), relation))
            if _is_var(pattern.object) and pattern.object in new_bindings:
                grounded_requirements.add(("object", _norm(new_bindings[pattern.object]), relation))
        matches = [
            claim
            for claim in self.state.deferred.values()
            if (
                ("subject", _norm(claim.subject), _norm(claim.relation)) in grounded_requirements
                or ("object", _norm(claim.object), _norm(claim.relation)) in grounded_requirements
            )
        ]
        self._emit(
            "DEFERRED_LOOKUP",
            {
                "claim_ids": [claim.claim_id for claim in matches],
                "bindings": new_bindings,
                "hit": bool(matches),
                "requirement_count": len(grounded_requirements),
            },
        )
        return matches

    def apply_verification(self, result: VerifyResult) -> Claim | None:
        origin = "DEFERRED" if result.claim_id in self.state.deferred else (
            "PENDING" if result.claim_id in self.state.pending else "VERIFIED"
        )
        claim = (
            self.state.deferred.get(result.claim_id)
            or self.state.pending.get(result.claim_id)
            or self.state.verified.get(result.claim_id)
        )
        if claim is None:
            raise KeyError(result.claim_id)
        claim.verification = result.status
        if result.normalized_claim is not None:
            claim = result.normalized_claim
        if result.status == VerifyStatus.ACCEPT:
            claim.disposition = Disposition.PROMOTED
            self.state.deferred.pop(result.claim_id, None)
            self.state.pending.pop(result.claim_id, None)
            self.state.verified[claim.claim_id] = claim
            promoted_event = self._emit(
                "CLAIM_PROMOTED",
                {"claim": claim.model_dump(mode="json"), "origin": origin},
            )
            new_bindings = self._bind_from_claim(claim)
            for variable, value in new_bindings.items():
                self._emit(
                    "ENTITY_BOUND",
                    {"variable": variable, "value": value},
                    caused_by_event_seq=promoted_event.event_seq,
                )
            self._verification_matches.extend(self._lookup_deferred(new_bindings))
        elif result.status == VerifyStatus.REJECT:
            claim.disposition = Disposition.REJECTED
            self.state.deferred.pop(result.claim_id, None)
            self.state.pending.pop(result.claim_id, None)
            self.state.rejected[claim.claim_id] = claim
            self._emit("VERIFY_REJECTED", {"claim": claim.model_dump(mode="json"), "reason": result.reason})
        elif result.status == VerifyStatus.CONFLICT:
            claim.disposition = Disposition.REJECTED
            self.state.conflicts.setdefault(claim.claim_id, []).append(result.reason or "VERIFY_CONFLICT")
            self._emit("VERIFY_CONFLICT", {"claim": claim.model_dump(mode="json"), "reason": result.reason})
        else:
            self._emit("VERIFY_NEED_MORE_CONTEXT", {"claim_id": result.claim_id, "reason": result.reason})
        return claim

    def drain_verification_matches(self) -> list[Claim]:
        """Return deferred claims activated by the last accepted verification."""
        matches = self._verification_matches
        self._verification_matches = []
        return matches

    def evidence_pack(self) -> EvidencePack:
        target = self.state.plan.answer_contract.target if self.state.plan.answer_contract else None
        return EvidencePack(
            claims=list(self.state.verified.values()),
            operator_trace=list(self.state.operator_trace),
            answer_value=self.state.bindings.get(target) if target else None,
            answer_type=self.state.plan.answer_contract.type if self.state.plan.answer_contract else None,
        )

    def finalize(self) -> EvidencePack | None:
        """Run the EOS-only sufficiency gate and produce canonical evidence."""
        satisfied = {
            claim.matched_pattern_id
            for claim in self.state.verified.values()
            if claim.matched_pattern_id is not None
        }
        required = {pattern.id for pattern in self.state.plan.patterns if pattern.required}
        reasons: list[str] = []
        missing = sorted(required - satisfied)
        if missing:
            reasons.append("REQUIRED_PATTERN_MISSING:" + ",".join(missing))
        if self.state.conflicts:
            reasons.append("UNRESOLVED_CONFLICT")
        contract = self.state.plan.answer_contract
        if reasons:
            self.state.status = (
                RuntimeStatus.CONFLICTED
                if "UNRESOLVED_CONFLICT" in reasons
                else RuntimeStatus.INSUFFICIENT
            )
            self.state.reason_codes = reasons
            self._emit(
                "SUFFICIENCY_CHECKED",
                {"sufficient": False, "reason_codes": reasons},
            )
            self._emit(
                "RUN_STATUS",
                {"status": self.state.status.value, "reason_codes": reasons},
            )
            return None
        try:
            self._execute_plan_operators()
        except OperatorError as exc:
            self.state.status = RuntimeStatus.UNSUPPORTED
            self.state.reason_codes = [f"OPERATOR_ERROR:{exc}"]
            self._emit(
                "RUN_STATUS",
                {"status": self.state.status.value, "reason_codes": self.state.reason_codes},
            )
            return None
        if contract and contract.target and contract.target not in self.state.bindings:
            self.state.status = RuntimeStatus.INSUFFICIENT
            self.state.reason_codes = ["ANSWER_TARGET_UNBOUND:" + contract.target]
            self._emit(
                "RUN_STATUS",
                {"status": self.state.status.value, "reason_codes": self.state.reason_codes},
            )
            return None
        self.state.status = RuntimeStatus.ANSWERED
        self.state.reason_codes = []
        pack = self.evidence_pack()
        self._emit(
            "SUFFICIENCY_CHECKED",
            {"sufficient": True, "claim_ids": [claim.claim_id for claim in pack.claims]},
        )
        self._emit(
            "RUN_STATUS",
            {
                "status": self.state.status.value,
                "reason_codes": [],
                "answer_value": pack.answer_value,
            },
        )
        return pack

    def _execute_plan_operators(self) -> None:
        """Execute the small deterministic operator registry at EOS.

        Operators are declarative: inputs can reference bindings or literal
        lists, and an optional ``output``/``result`` parameter writes a derived
        value back into Runtime bindings for the answer contract.
        """
        if self._operators_executed:
            return
        for operator in self.state.plan.operators:
            target: str | None = None
            value: Any = None
            operator_type = operator.type.upper()
            if operator_type == "PROJECT":
                target = operator.params.get("target") or operator.params.get("output")
                source = operator.params.get("source") or (operator.inputs[0] if operator.inputs else None)
                if not target or not isinstance(source, str) or not _is_var(source):
                    raise OperatorError("PROJECT requires source and output variables")
                if source not in self.state.bindings:
                    raise OperatorError(f"PROJECT source is unbound: {source}")
                value = self.state.bindings[source]
            elif operator_type in {"DIRECT", "PATH_JOIN", "REGISTERED_RULE", "RULE", "FILTER"}:
                # These are structural declarations consumed by UPDATE; the
                # Runtime records them but does not invent derived facts.
                self.state.operator_trace.append(
                    {"operator_id": operator.id, "type": operator_type, "inputs": operator.inputs, "output": None, "value": None, "status": "DECLARED"}
                )
                self._emit("OPERATOR_EXECUTED", self.state.operator_trace[-1])
                continue
            else:
                args = [self._resolve_operator_value(value) for value in operator.inputs]
                if operator_type == "COUNT":
                    values = args[0] if len(args) == 1 and isinstance(args[0], list) else args
                    value = execute_operator(operator_type, values)
                elif operator_type in {"INTERSECTION", "UNION"}:
                    value = execute_operator(
                        operator_type,
                        args[0] if isinstance(args[0], list) else [args[0]],
                        args[1] if len(args) > 1 and isinstance(args[1], list) else args[1:],
                    )
                elif operator_type == "COMPARE":
                    if len(args) != 2:
                        raise OperatorError("COMPARE requires exactly two inputs")
                    value = execute_operator(
                        operator_type,
                        args[0],
                        args[1],
                        operator.params.get("comparison", operator.params.get("operator", "EQ")),
                    )
                elif operator_type in {"ARGMIN", "ARGMAX"}:
                    candidates = operator.params.get("candidates")
                    if not isinstance(candidates, list):
                        raise OperatorError(f"{operator_type} requires params.candidates")
                    resolved_candidates = [self._resolve_operator_value(item) for item in candidates]
                    value = execute_operator(operator_type, args, resolved_candidates)
                else:
                    value = execute_operator(operator_type, *args)
                target = operator.params.get("output") or operator.params.get("output_var") or operator.params.get("result")
            if target:
                if not isinstance(target, str) or not _is_var(target):
                    raise OperatorError(f"operator output must be a variable: {target!r}")
                self.state.bindings[target] = value
            trace = {
                "operator_id": operator.id,
                "type": operator_type,
                "inputs": operator.inputs,
                "output": target,
                "value": value,
                "status": "EXECUTED",
            }
            self.state.operator_trace.append(trace)
            executed_event = self._emit("OPERATOR_EXECUTED", trace)
            if target:
                self._emit(
                    "ENTITY_BOUND",
                    {"variable": target, "value": value, "binding_source": "operator"},
                    caused_by_event_seq=executed_event.event_seq,
                )
        self._operators_executed = True

    def _resolve_operator_value(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._resolve_operator_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._resolve_operator_value(item) for key, item in value.items()}
        if isinstance(value, str) and _is_var(value):
            if value not in self.state.bindings:
                raise OperatorError(f"operator input is unbound: {value}")
            return self.state.bindings[value]
        return value
