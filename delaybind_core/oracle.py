"""Loading and validating externally supplied Oracle QueryPlans."""

from __future__ import annotations

import json
import re
import hashlib
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .data import CanonicalSample
from .plan_validation import PlanValidationError, ensure_valid_plan
from .profiler import infer_answer_contract
from .relation_semantics import relation_signature
from .schema import OperatorSpec, Pattern, QueryPlan, RelationSpec


class OraclePlanError(ValueError):
    """Raised when an Oracle Plan bundle cannot be indexed safely."""


def _plan_items(parsed: Any) -> list[tuple[str, Any]]:
    if isinstance(parsed, dict) and "plans" in parsed:
        parsed = parsed["plans"]
    if isinstance(parsed, dict):
        if "plan_id" in parsed and "patterns" in parsed:
            sample_id = str(parsed.get("sample_id") or parsed.get("id") or "")
            if not sample_id:
                raise OraclePlanError("a single plan object must include sample_id")
            return [(sample_id, parsed)]
        return [(str(sample_id), plan) for sample_id, plan in parsed.items()]
    if isinstance(parsed, list):
        result: list[tuple[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                raise OraclePlanError("plan bundle list entries must be objects")
            plan = item.get("plan", item)
            sample_id = item.get("sample_id") or item.get("id") or plan.get("sample_id")
            if not sample_id:
                raise OraclePlanError("each plan bundle entry must include sample_id")
            result.append((str(sample_id), plan))
        return result
    raise OraclePlanError("Oracle Plan file must be a plan, mapping, list, or {plans: ...}")


def _without_bundle_metadata(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key not in {"sample_id", "id"}}


def load_oracle_plans(path: str | Path) -> dict[str, QueryPlan]:
    """Load JSON/JSONL plans keyed by canonical sample ID.

    Accepted JSON forms are ``{sample_id: plan}``, ``[{sample_id, plan}]`` and
    ``{plans: [...]}``; JSONL uses one ``{sample_id, plan}`` object per line.
    Every plan is schema- and semantically validated before being returned.
    """
    path = Path(path)
    if not path.exists():
        raise OraclePlanError(f"Oracle Plan file does not exist: {path}")
    if path.suffix == ".jsonl":
        parsed: Any = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    elif path.suffix == ".json":
        parsed = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise OraclePlanError(f"unsupported Oracle Plan format: {path.suffix}")
    plans: dict[str, QueryPlan] = {}
    for sample_id, raw_plan in _plan_items(parsed):
        try:
            plan = QueryPlan.model_validate(_without_bundle_metadata(raw_plan))
        except Exception as exc:
            raise OraclePlanError(f"invalid Oracle Plan for {sample_id}: {exc}") from exc
        try:
            ensure_valid_plan(plan)
        except PlanValidationError as exc:
            raise OraclePlanError(f"semantically invalid Oracle Plan for {sample_id}: {exc}") from exc
        if sample_id in plans:
            raise OraclePlanError(f"duplicate Oracle Plan for sample {sample_id}")
        plans[sample_id] = plan
    return plans


def write_oracle_plans(plans: dict[str, QueryPlan], path: str | Path) -> None:
    """Write a deterministic, human-reviewable JSON Oracle Plan bundle."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        sample_id: plan.model_dump(mode="json")
        for sample_id, plan in sorted(plans.items())
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mentioned(value: str, question: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(value.casefold())}(?!\w)", question.casefold()) is not None


def _key_mentioned(value: str, question: str) -> bool:
    """Match an entity while ignoring quote/case/punctuation differences."""
    value_key = _entity_key(value)
    question_key = _entity_key(question)
    return re.search(rf"(?<!\w){re.escape(value_key)}(?!\w)", question_key) is not None


def _question_surface_with_disambiguator(value: str, question: str) -> str | None:
    match = re.search(rf"(?<!\w){re.escape(value)}(?!\w)", question, re.IGNORECASE)
    if match is None:
        return None
    suffix = question[match.end():]
    parenthetical = re.match(r"\s*\([^)]{1,80}\)", suffix)
    end = match.end() + (parenthetical.end() if parenthetical else 0)
    return question[match.start():end]


def _entity_key(value: str) -> str:
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value.strip().strip('"\''))
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _best_question_anchor(value: str, question: str) -> str:
    tokens = list(re.finditer(r"\w+(?:['’-]\w+)*", question, re.UNICODE))
    target = _entity_key(value)
    target_tokens = set(target.split())
    best: tuple[float, str] | None = None
    max_width = min(len(tokens), max(1, len(target_tokens) + 4))
    for width in range(1, max_width + 1):
        for start in range(0, len(tokens) - width + 1):
            end = start + width - 1
            phrase = question[tokens[start].start():tokens[end].end()]
            normalized = _entity_key(phrase)
            phrase_tokens = set(normalized.split())
            overlap = len(target_tokens & phrase_tokens) / max(1, len(target_tokens | phrase_tokens))
            score = SequenceMatcher(None, target, normalized).ratio() + 0.35 * overlap
            candidate = (score, phrase)
            if best is None or candidate[0] > best[0] or (
                candidate[0] == best[0] and len(candidate[1]) > len(best[1])
            ):
                best = candidate
    if best is None or best[0] < 0.35:
        raise OraclePlanError(f"cannot align evidence entity {value!r} to the question")
    return best[1]


def _alias_score(left: str, right: str) -> float:
    left_key, right_key = _entity_key(left), _entity_key(right)
    left_tokens, right_tokens = set(left_key.split()), set(right_key.split())
    overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    return SequenceMatcher(None, left_key, right_key).ratio() + 0.5 * overlap


def _variable_name(relation: str, used: set[str]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "_", relation.casefold()).strip("_") or "value"
    stem = stem[:24]
    candidate = f"?{stem}"
    index = 2
    while candidate in used:
        candidate = f"?{stem}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _comparison_operator(question: str) -> str:
    folded = question.casefold()
    if re.search(r"\b(younger|later|last|after|newer|latest|most recent)\b", folded):
        return "ARGMAX"
    return "ARGMIN"


def compile_oracle_plan(sample: CanonicalSample) -> QueryPlan:
    """Compile a gold-only Oracle Plan from 2Wiki evidence triples.

    The compiler uses gold evidence topology and the gold answer only to select
    an output variable. Concrete non-question values are replaced by variables,
    so the resulting plan cannot reveal the answer to UPDATE. This function is
    strictly for Oracle experiments and supervision generation, never predicted
    test-time planning.
    """
    triples = [tuple(item[:3]) for item in sample.evidences if len(item) >= 3]
    if not triples:
        raise OraclePlanError(f"sample {sample.sample_id} has no evidence triples")
    comparison = "comparison" in (sample.question_type or "").casefold()
    supporting_titles = {
        index: str(sample.supporting_facts[index][0])
        for index in range(min(len(triples), len(sample.supporting_facts)))
    }
    subject_support_titles = {
        _entity_key(str(triples[index][0])): title
        for index, title in supporting_titles.items()
    }
    alias_keys: dict[str, str] = {}
    earlier_objects: list[str] = []
    for subject, _, obj in triples:
        subject_text = str(subject)
        subject_key = _entity_key(subject_text)
        if subject_key not in {_entity_key(value) for value in earlier_objects} and not _mentioned(
            subject_text, sample.question
        ) and earlier_objects:
            best_object = max(
                earlier_objects,
                key=lambda value: _alias_score(subject_text, value),
            )
            if _alias_score(subject_text, best_object) >= 0.25:
                alias_keys[subject_key] = alias_keys.get(_entity_key(best_object), _entity_key(best_object))
        earlier_objects.append(str(obj))

    def key(value: str) -> str:
        raw = _entity_key(value)
        while raw in alias_keys and alias_keys[raw] != raw:
            raw = alias_keys[raw]
        return raw

    evidence_subjects = {key(str(subject)) for subject, _, _ in triples}
    evidence_objects = {key(str(obj)) for _, _, obj in triples}
    root_entities = evidence_subjects - evidence_objects
    used_variables: set[str] = set()
    value_slots: dict[str, str] = {}
    representatives: dict[str, str] = {}
    for subject, _, obj in triples:
        subject_text = str(subject)
        support_title = subject_support_titles.get(_entity_key(subject_text))
        representative = (
            support_title
            if support_title and _key_mentioned(support_title, sample.question)
            else subject_text
        )
        representatives.setdefault(key(subject_text), representative)
        representatives.setdefault(key(str(obj)), str(obj))

    for subject, relation, obj in triples:
        for value, role in ((str(subject), "subject"), (str(obj), "object")):
            node_key = key(value)
            if node_key in root_entities:
                value_slots.setdefault(node_key, representatives[node_key])
                continue
            if _mentioned(value, sample.question):
                value_slots.setdefault(node_key, value)
                continue
            if comparison and role == "object" and node_key not in evidence_subjects:
                # Comparison values must remain independent even when their
                # hidden gold values happen to be equal; sharing a variable
                # here would leak the Yes/No answer into the Oracle topology.
                continue
            hint = str(relation) if role == "object" else "entity"
            if node_key not in value_slots:
                value_slots[node_key] = _variable_name(hint, used_variables)

    patterns: list[Pattern] = []
    for index, (subject, relation, obj) in enumerate(triples, start=1):
        object_value = str(obj)
        subject_key = key(str(subject))
        object_key = key(object_value)
        object_slot = value_slots.get(object_key)
        if object_slot is None:
            object_slot = _variable_name(str(relation), used_variables)
        subject_slot = value_slots[subject_key]
        qualifiers: dict[str, Any] = {}
        support_title = supporting_titles.get(index - 1)
        subject_surface = (
            _question_surface_with_disambiguator(subject_slot, sample.question)
            if not subject_slot.startswith("?")
            else None
        )
        if subject_surface and subject_surface.casefold().strip() != subject_slot.casefold().strip():
            qualifiers["subject_question_anchor"] = subject_surface
            qualifiers["subject_aliases"] = [subject_surface]
        if not subject_slot.startswith("?") and not _mentioned(subject_slot, sample.question):
            anchor = (
                _question_surface_with_disambiguator(support_title, sample.question)
                if support_title
                else None
            ) or _best_question_anchor(subject_slot, sample.question)
            qualifiers["subject_question_anchor"] = anchor
            qualifiers["subject_aliases"] = [anchor]
        if not subject_slot.startswith("?"):
            aliases = qualifiers.setdefault("subject_aliases", [])
            for alias in (str(subject), support_title):
                if alias and alias.casefold().strip() != subject_slot.casefold().strip() and alias not in aliases:
                    aliases.append(alias)
            if not aliases:
                qualifiers.pop("subject_aliases", None)
        object_surface = (
            _question_surface_with_disambiguator(object_slot, sample.question)
            if not object_slot.startswith("?")
            else None
        )
        if object_surface and object_surface.casefold().strip() != object_slot.casefold().strip():
            qualifiers["object_question_anchor"] = object_surface
            qualifiers["object_aliases"] = [object_surface]
        if not object_slot.startswith("?") and not _mentioned(object_slot, sample.question):
            anchor = _best_question_anchor(object_slot, sample.question)
            qualifiers["object_question_anchor"] = anchor
            qualifiers["object_aliases"] = [anchor]
        patterns.append(
            Pattern(
                id=f"p{index:02d}",
                subject=subject_slot,
                relation=str(relation),
                relation_family=str(relation),
                object=object_slot,
                qualifiers=qualifiers,
            )
        )
    consumed_variables = {
        pattern.subject for pattern in patterns if pattern.subject.startswith("?")
    }
    patterns = [
        pattern.model_copy(
            update={
                "qualifiers": {
                    **pattern.qualifiers,
                    "binding_policy": "PATH_CONSISTENT",
                    "candidate_output": pattern.object,
                }
            }
        )
        if pattern.object.startswith("?") and pattern.object in consumed_variables
        else pattern
        for pattern in patterns
    ]
    relation_specs = []
    for index, relation in enumerate(dict.fromkeys(str(item[1]) for item in triples), start=1):
        signature = relation_signature(relation)
        relation_specs.append(
            RelationSpec(
                id=f"r{index:02d}",
                description=f"Gold evidence relation: {relation}",
                relation_family=relation,
                subject_type=signature.subject_type,
                object_type=signature.object_type,
                direction="subject_to_object",
            )
        )
    operators: list[OperatorSpec] = []
    contract = infer_answer_contract(sample)

    if comparison:
        terminal = [
            (index, triple)
            for index, triple in enumerate(triples)
            if key(str(triple[2])) not in evidence_subjects
        ]
        if len(terminal) < 2:
            raise OraclePlanError(
                f"comparison sample {sample.sample_id} has fewer than two terminal evidence values"
            )
        answer_target = "?answer"
        used_variables.add(answer_target)
        if contract.type == "BOOLEAN":
            operators.append(
                OperatorSpec(
                    id="compare_answer",
                    type="COMPARE",
                    inputs=[patterns[index].object for index, _ in terminal[:2]],
                    params={"comparison": "EQ", "output": answer_target},
                )
            )
            contract = contract.model_copy(update={"target": answer_target, "type": "BOOLEAN"})
        else:
            predecessors: dict[str, list[str]] = {}
            for subject, _, obj in triples:
                predecessors.setdefault(key(str(obj)), []).append(
                    key(str(subject))
                )

            def question_root(value: str, seen: set[str] | None = None) -> str:
                value = key(value)
                if not value_slots[value].startswith("?"):
                    return value_slots[value]
                seen = set() if seen is None else seen
                if value in seen:
                    return value_slots[value]
                seen.add(value)
                for predecessor in predecessors.get(value, []):
                    root = question_root(predecessor, seen)
                    if not root.startswith("?"):
                        return root
                return value_slots[value]

            operators.append(
                OperatorSpec(
                    id="select_answer",
                    type=_comparison_operator(sample.question),
                    inputs=[patterns[index].object for index, _ in terminal],
                    params={
                        "candidates": [
                            question_root(str(triple[0])) for _, triple in terminal
                        ],
                        "output": answer_target,
                    },
                )
            )
            contract = contract.model_copy(update={"target": answer_target, "type": "ENTITY"})
    else:
        answer_value = str(sample.answer) if sample.answer is not None else str(triples[-1][2])
        answer_target = value_slots.get(key(answer_value))
        if answer_target is None or not answer_target.startswith("?"):
            answer_target = value_slots[key(str(triples[-1][2]))]
        contract = contract.model_copy(update={"target": answer_target})

    digest = hashlib.sha256(sample.sample_id.encode("utf-8")).hexdigest()[:12]
    plan = QueryPlan(
        plan_id=f"oracle-{digest}",
        patterns=patterns,
        relation_specs=relation_specs,
        operators=operators,
        constraints={"source": "2wiki_gold_evidences", "gold_only": True},
        answer_contract=contract,
    )
    try:
        return ensure_valid_plan(plan, question=sample.question)
    except PlanValidationError as exc:
        raise OraclePlanError(f"compiled Oracle Plan failed for {sample.sample_id}: {exc}") from exc


def compile_oracle_plans(samples: Iterable[CanonicalSample]) -> dict[str, QueryPlan]:
    plans: dict[str, QueryPlan] = {}
    for sample in samples:
        plans[sample.sample_id] = compile_oracle_plan(sample)
    return plans
