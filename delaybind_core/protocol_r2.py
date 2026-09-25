"""Text wire adapters for R2.

The state machine remains strict about authority, while harmless presentation
differences are normalized at the boundary.  In particular, model-authored
reasons and proof-kind labels are never required for a transaction.
"""

import re

from .schema_r2 import (EvidenceReview, MemoryResponseR2, UpdateResponseR2, Observation, Hint,
                        LEGACY_MEMORY_INTERFACE, UNIFIED_MEMORY_INTERFACE)
from .text_protocol_v52 import fields, refs, value, parse_plan
from .source_refs_v52 import SentenceRefResolver


def fact_alias_map(fact_ids):
    """Return per-request short model IDs mapped to durable runtime fact IDs."""
    return {f"F{index}": fact_id for index, fact_id in enumerate(sorted(set(fact_ids)), start=1)}


def unified_fact_aliases(context):
    """Validate the same frozen fact whitelist for both rendering and parsing.

    Unified requests authorize only short IDs actually accompanied by a fact
    body. Durable IDs remain runtime metadata and are not accepted on the wire.
    """
    get = context.get if isinstance(context, dict) else lambda key, default=None: getattr(context, key, default)
    aliases = dict(get("fact_aliases", {}))
    allowed = list(get("allowed_fact_ids", []))
    if (len(allowed) != len(set(allowed)) or len(aliases) != len(allowed)
            or set(aliases.values()) != set(allowed)
            or any(not re.fullmatch(r"F[1-9][0-9]*", alias) for alias in aliases)):
        raise ValueError("UNIFIED_FACT_ALIAS_WHITELIST_MISMATCH")
    pack = get("working_memory")
    navigation = (pack.get("navigation", {}) if isinstance(pack, dict)
                  else getattr(pack, "navigation", {})) or {}
    facts = navigation.get("facts", [])
    if (len(facts) != len(allowed) or {item.get("fact_id") for item in facts} != set(allowed)
            or any(not isinstance(item.get("text"), str) or not item["text"].strip() for item in facts)):
        raise ValueError("UNIFIED_FACT_DISPLAY_WHITELIST_MISMATCH")
    return aliases


def typed_value(text):
    """Only value fields accept typed literals; never silently drop duplicate keys."""
    import json
    def unique(items):
        result = {}
        for key, val in items:
            if key in result:
                raise ValueError("DUPLICATE_VALUE_KEY")
            result[key] = val
        return result
    def invalid_constant(_):
        raise ValueError("NONFINITE_BINDING_VALUE")
    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    except json.JSONDecodeError:
        if text.lstrip().startswith(("{", "[", '"')):
            raise ValueError("INVALID_TYPED_VALUE")
        return text


def _looks_like_joined_values(value: str, support_texts: list[str]) -> bool:
    """Reject only conspicuous attempts to smuggle a collection into SINGLE.

    Natural single entities may contain a conjunction (for example,
    ``Trinidad and Tobago``), so a bare ``and`` is not sufficient. Multiple
    Separate atomic supports for the two sides, or two person-like multi-token
    names, is a high-confidence protocol violation. SET values use an array.
    """
    text = " ".join(value.split())
    if any(separator in text for separator in (";", "；", "、")):
        return True
    conjunction = re.search(r"\s+(?:and|or|和|与|以及)\s+", text, flags=re.I)
    if not conjunction:
        return False
    left, right = text[:conjunction.start()], text[conjunction.end():]
    left_key, right_key = left.strip(" ,").casefold(), right.strip(" ,").casefold()
    normalized_support = [item.casefold() for item in support_texts]
    left_only = any(left_key in item and right_key not in item for item in normalized_support)
    right_only = any(right_key in item and left_key not in item for item in normalized_support)
    if left_only and right_only:
        return True
    title_words = lambda part: re.findall(r"(?:^|\s)[A-Z][A-Za-z.'’-]*", part)
    return len(title_words(left)) >= 2 and len(title_words(right)) >= 2


def strict_lines(raw):
    if not isinstance(raw, str) or not raw.strip() or raw.lstrip().startswith(("{", "[", "```")):
        raise ValueError("R2_REQUIRES_TEXT_BLOCKS")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _visible_refs(text, context):
    """Recover only exact, already-authorized source IDs from a wire field.

    Older model responses sometimes appended an explanation to the final
    source field (``D1:S0 directly states ...``).  The explanation has no
    authority, so retain only visible anchors and discard the tail.
    """
    if text in {"", "NONE"}:
        return []
    direct = refs(text)
    allowed = set(context.visible_source_refs)
    if direct and set(direct) <= allowed:
        return direct
    found = []
    for source_ref in sorted(allowed, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(source_ref)}(?![A-Za-z0-9_])", text):
            found.append(source_ref)
    return sorted(found)


def _review_reason(verdict):
    return {
        "ACCEPT": "Runtime accepted the reviewed source support.",
        "REJECT": "Runtime recorded the review rejection.",
        "HOLD": "Runtime recorded insufficient review context.",
        "CONFLICT": "Runtime recorded an unresolved source conflict.",
    }.get(verdict, "Runtime recorded the evidence review.")


def parse_memory(raw, context):
    interface = getattr(context, "memory_interface", LEGACY_MEMORY_INTERFACE)
    if interface == UNIFIED_MEMORY_INTERFACE:
        return parse_unified_memory(raw, context)
    if interface != LEGACY_MEMORY_INTERFACE:
        raise ValueError("UNKNOWN_MEMORY_INTERFACE:" + str(interface))
    # This alias applies to the entire fact-only FINAL reply, never to a
    # support field or an individual line within a multi-member response.
    if context.fact_only and context.phase == "FINAL" and isinstance(raw, str) and raw.strip() == "NONE":
        response = parse_memory("NOOP", context)
        response.format_normalizations.append(dict(
            reason="STANDALONE_NONE_AS_NOOP", original_response=raw, normalized_response="NOOP"))
        return response
    rows = strict_lines(raw)
    if context.member_bindings and context.phase == "FINAL":
        return _parse_member_memory(rows, context)
    if context.fact_only:
        if context.phase != "FINAL":
            raise ValueError("FACT_ONLY_MEMORY_MUST_BE_FINAL")
        if len(rows) != 1:
            raise ValueError("FACT_ONLY_MEMORY_REQUIRES_ONE_RESULT")
        f = fields(rows[0])
        if f[0] == "BOUND":
            if len(f) != 3:
                raise ValueError(f"FACT_ONLY_BOUND_FIELD_COUNT:expected=3,received={len(f)}")
            aliases = fact_alias_map(context.allowed_fact_ids)
            allowed = set(context.allowed_fact_ids)
            wire_support = refs(f[2])
            unknown = [item for item in wire_support if item not in aliases and item not in allowed]
            if unknown:
                raise ValueError("UNKNOWN_FACT_ALIAS:" + ",".join(unknown))
            # Accept durable IDs for backward compatibility, but production
            # prompts expose only short, request-local aliases.
            support = [aliases.get(item, item) for item in wire_support]
            if len(support) != len(set(support)):
                raise ValueError("DUPLICATE_SUPPORT_FACT")
            parsed_value = typed_value(f[1])
            visible_facts = {item.get("fact_id"): item.get("text", "")
                             for item in context.working_memory.navigation.get("facts", [])}
            if (context.expected_cardinality == "SINGLE" and isinstance(parsed_value, str)
                    and _looks_like_joined_values(parsed_value, [visible_facts.get(fid, "") for fid in support])):
                raise ValueError("SINGLE_VALUE_CONTAINS_MULTIPLE_VALUES:use_one_value_or_fix_plan_cardinality")
            result = dict(state="BOUND", value=parsed_value, kind="DIRECT" if support else "INFERRED",
                          support_fact_ids=support, decision_source_refs=[], reason_code="FACT_SUPPORTED",
                          reason="Runtime accepted the selected working-memory facts.")
        elif f[0] in {"NOOP", "UNBOUND"}:
            if len(f) != 1:
                raise ValueError(f"FACT_ONLY_NOOP_FIELD_COUNT:expected=1,received={len(f)}")
            result = dict(state="NOOP", value=None, support_fact_ids=[], decision_source_refs=[],
                          reason_code="NO_BINDING_CHANGE",
                          reason="Runtime left facts and bindings unchanged.")
        else:
            raise ValueError(f"INVALID_FACT_ONLY_MEMORY_FIELD:{f[0]}")
        return MemoryResponseR2(context_id=context.context_id, review_id=context.review_id,
            operations=[dict(op=context.allowed_mode, query_id=context.query_id,
                             evidence_reviews=[], binding_result=result)])
    reviews, result, result_seen, ignored = {}, None, False, []
    for row in rows:
        f = fields(row)
        op = f[0]
        # Phase belongs to the runtime.  A recognizable command emitted in the
        # wrong phase has no authority and is ignored instead of invalidating
        # valid lines from the requested phase.
        if context.phase == "REVIEW" and op in {"BOUND", "UNBOUND", "FINAL"}:
            ignored.append({"line": row, "reason": "RESULT_IGNORED_DURING_REVIEW"})
            continue
        if context.phase == "FINAL" and op in {"REVIEW", "CORRECTION", "CONTEXT"}:
            ignored.append({"line": row, "reason": "REVIEW_IGNORED_DURING_FINAL"})
            continue
        if f[0] == "REVIEW":
            if len(f) not in {4, 5}:
                raise ValueError(f"REVIEW_FIELD_COUNT:expected=4_or_5,received={len(f)}")
            if f[1] in reviews:
                raise ValueError("DUPLICATE_EVIDENCE_REVIEW")
            # A legacy fifth reason field is accepted for compatibility but is
            # intentionally not persisted as model-owned runtime state.
            reviews[f[1]] = dict(fact_id=f[1], verdict=f[2], checked_refs=_visible_refs(f[3], context),
                                 reason=_review_reason(f[2]))
        elif f[0] == "CORRECTION":
            if len(f) != 5:
                raise ValueError(f"CORRECTION_FIELD_COUNT:expected=5,received={len(f)}")
            if f[1] not in reviews or "correction" in reviews[f[1]]:
                raise ValueError("CORRECTION_WITHOUT_UNIQUE_REVIEW")
            reviews[f[1]]["correction"] = dict(local_id=f[2], source_refs=refs(f[3]), text=f[4])
        elif f[0] == "CONTEXT":
            if len(f) != 5:
                raise ValueError(f"CONTEXT_FIELD_COUNT:expected=5,received={len(f)}")
            if f[1] not in reviews or "context_request" in reviews[f[1]]:
                raise ValueError("CONTEXT_WITHOUT_UNIQUE_REVIEW")
            reviews[f[1]]["context_request"] = dict(anchor=f[2], before=int(f[3]), after=int(f[4]))
        elif f[0] == "BOUND":
            if result_seen:
                raise ValueError("DUPLICATE_BINDING_RESULT")
            result_seen = True
            if len(f) == 4:
                support_field, source_field = f[2], f[3]
            elif len(f) in {5, 6} and f[2] in {"DIRECT", "INFERRED"}:
                # Backward-compatible five/six-field form.  The supplied kind
                # and optional reason are ignored; both are runtime-owned.
                support_field, source_field = f[3], f[4]
            elif len(f) == 5:
                # Transitional four-field form with an obsolete reason tail.
                support_field, source_field = f[2], f[3]
            else:
                raise ValueError(f"BOUND_FIELD_COUNT:expected=4_or_legacy_5_or_6,received={len(f)}")
            support = refs(support_field)
            result = dict(state="BOUND", value=typed_value(f[1]), kind="DIRECT" if support else "INFERRED",
                          support_fact_ids=support, decision_source_refs=_visible_refs(source_field, context),
                          reason_code="SUPPORTED", reason="Runtime accepted the authorized binding proof.")
        elif f[0] == "UNBOUND":
            if result_seen:
                raise ValueError("DUPLICATE_BINDING_RESULT")
            result_seen = True
            if len(f) not in {2, 3}:
                raise ValueError(f"UNBOUND_FIELD_COUNT:expected=2_or_legacy_3,received={len(f)}")
            result = dict(state="UNBOUND", value=None, support_fact_ids=[],
                          decision_source_refs=_visible_refs(f[1], context),
                          reason_code="INSUFFICIENT_EVIDENCE",
                          reason="Runtime could not establish an authorized binding proof.")
        else:
            raise ValueError(f"INVALID_R2_MEMORY_FIELD:{f[0]}")
    if context.phase == "FINAL" and not result_seen:
        raise ValueError("MEMORY_FINAL_REQUIRES_RESULT")
    reason_codes = {
        "ACCEPT": lambda review: "CORRECTED" if review.get("correction") else "RAW_SUPPORTED",
        "REJECT": lambda _review: "REJECTED_BY_REVIEW",
        "HOLD": lambda review: "NEED_CONTEXT" if review.get("context_request") else "INSUFFICIENT_CONTEXT",
        "CONFLICT": lambda _review: "SOURCE_CONFLICT",
    }
    for review in reviews.values():
        try:
            review["reason_code"] = reason_codes[review["verdict"]](review)
        except KeyError as exc:
            raise ValueError(f"INVALID_REVIEW_VERDICT:{review.get('verdict')}") from exc
    response = MemoryResponseR2(context_id=context.context_id, review_id=context.review_id, ignored_lines=ignored,
        operations=[dict(op=context.allowed_mode, query_id=context.query_id,
                         evidence_reviews=list(reviews.values()), binding_result=result)])
    return response


def parse_unified_memory(raw, context):
    """Parse a complete neutral answer set; never infer protocol or write mode."""
    from .schema_r2 import MemberResult
    from .member_graph_r2 import scalar_value, projected_value
    from .schema_v52 import digest

    if not context.fact_only or not context.member_bindings or context.phase != "FINAL":
        raise ValueError("UNIFIED_MEMORY_REQUIRES_FACT_ONLY_MEMBER_FINAL")
    aliases = unified_fact_aliases(context)
    rows = strict_lines(raw)
    parsed = []
    for row in rows:
        parts = fields(row)
        if len(parts) != 3:
            raise ValueError(f"UNIFIED_MEMORY_FIELD_COUNT:expected=3,received={len(parts)}")
        if parts[0] != context.query_id:
            raise ValueError("UNIFIED_MEMORY_QUERY_MISMATCH:" + parts[0])
        parsed.append(parts)

    unknown = [parts for parts in parsed if parts[2] == "UNKNOWN"]
    if unknown:
        if len(parsed) != 1 or unknown[0][1] != "NONE":
            raise ValueError("UNIFIED_UNKNOWN_REQUIRES_STANDALONE_NONE_SUPPORT")
        result = dict(state="NOOP", reason_code="UNSUPPORTED_RESULT",
                      reason="The supplied evidence did not support a result in this review.")
    else:
        members = {}
        for _, support_text, answer in parsed:
            if support_text == "NONE":
                if not context.upstream_only_allowed:
                    raise ValueError("UNIFIED_MISSING_FACT_SUPPORT")
                support = []
            else:
                wire_support = [item.strip() for item in support_text.split(",")]
                if not wire_support or any(not item for item in wire_support):
                    raise ValueError("UNIFIED_INVALID_SUPPORT_FIELD")
                unknown_ids = [item for item in wire_support if item not in aliases]
                if unknown_ids:
                    raise ValueError("UNKNOWN_FACT_ALIAS:" + ",".join(unknown_ids))
                if len(wire_support) != len(set(wire_support)):
                    raise ValueError("DUPLICATE_SUPPORT_FACT")
                support = [aliases[item] for item in wire_support]
            try:
                member_value = scalar_value(typed_value(answer))
            except ValueError as exc:
                raise ValueError("UNIFIED_REQUIRES_ONE_CONCRETE_ANSWER:" + str(exc)) from exc
            key = digest(member_value)
            if key in members:
                members[key].support_fact_ids = sorted(set(members[key].support_fact_ids + support))
            else:
                members[key] = MemberResult(value=member_value, support_fact_ids=sorted(support))
        items = list(members.values())
        support = sorted({fid for item in items for fid in item.support_fact_ids})
        result = dict(state="BOUND", value=projected_value(items), members=items,
                      kind="DIRECT" if support else "INFERRED", support_fact_ids=support,
                      reason_code="SUPPORTED_RESULT",
                      reason="The supplied evidence supports the complete returned answer set.")
    return MemoryResponseR2(context_id=context.context_id, review_id=context.review_id,
        operations=[dict(op=context.allowed_mode, query_id=context.query_id,
                         evidence_reviews=[], binding_result=result)])


def _parse_member_memory(rows, context):
    """One text BOUND line per entity, each with its own proof; no arrays."""
    from .schema_r2 import MemberResult
    from .member_graph_r2 import scalar_value, projected_value
    if context.fact_only and "NONE" in rows:
        raise ValueError("STANDALONE_NONE_CANNOT_BE_MIXED_WITH_OTHER_MEMORY_LINES")
    legacy = context.model_copy(update={"member_bindings": False, "expected_cardinality": None})
    decisions, ignored = [], []
    for row in rows:
        head = fields(row)[0]
        if not context.fact_only and head in {"REVIEW", "CORRECTION", "CONTEXT"}:
            ignored.append(dict(line=row, reason="REVIEW_IGNORED_DURING_FINAL"))
            continue
        if row == "NOOP" and not context.fact_only:
            from .schema_r2 import BindingResult
            decisions.append(BindingResult(state="NOOP", reason_code="NO_BINDING_CHANGE",
                                           reason="No branch binding change."))
        else:
            parsed = parse_memory(row, legacy)
            decisions.append(parsed.operations[0].binding_result)
    if not decisions or len(decisions) > 1 and any(r.state != "BOUND" for r in decisions):
        raise ValueError("MEMBER_MEMORY_EXPECTS_BOUND_LINES_OR_ONE_NOOP")
    result = decisions[0].model_copy(deep=True)
    if result.state == "BOUND":
        items = [MemberResult(value=scalar_value(r.value), support_fact_ids=r.support_fact_ids,
                              decision_source_refs=r.decision_source_refs) for r in decisions]
        result.members = items
        result.value = projected_value(items)
        result.support_fact_ids = sorted({f for m in items for f in m.support_fact_ids})
        result.decision_source_refs = sorted({f for m in items for f in m.decision_source_refs})
    return MemoryResponseR2(context_id=context.context_id, review_id=context.review_id, ignored_lines=ignored,
        operations=[dict(op=context.allowed_mode, query_id=context.query_id,
                         evidence_reviews=[], binding_result=result)])


def parse_update(raw, context):
    """Validate each route independently, then coalesce immutable fact bodies."""
    rows = strict_lines(raw)
    valid, hints, rejected = {}, [], []
    if rows == ["NONE"]:
        return UpdateResponseR2(context_id=context["context_id"]), []
    status = {q["id"]: q["status"] for q in context["query_graph"]["queries"]}
    fact_only = bool(context.get("fact_only"))
    resolver = None if fact_only else SentenceRefResolver(context["visible_sources"])
    for row in rows:
        qid, text, source_refs = "", "", []
        try:
            f = fields(row)
            if f[0] == "PLAN_HINT" and ((fact_only and len(f) == 2) or (not fact_only and len(f) == 3)):
                hint = Hint(source_refs=[] if fact_only else refs(f[1]), text=f[1] if fact_only else f[2])
                if resolver is not None:
                    resolver.resolve(hint.source_refs)
                hints.append(hint)
                continue
            expected = 2 if fact_only else 3
            if len(f) != expected or "," in f[0]:
                raise ValueError("ONE_QUERY_ROUTE_PER_LINE_REQUIRED")
            if fact_only:
                qid, text = f
                source_refs = []
            else:
                qid, source, text = f
                source_refs = refs(source)
                resolver.resolve(source_refs)
            # In fact-only mode models sometimes emit a per-query empty row.
            # It carries no authority and is equivalent to omitting that row.
            if fact_only and text.strip() == "NONE":
                if qid not in status:
                    raise ValueError("UNKNOWN_QUERY_ID")
                continue
            if not text.strip() or text.strip() == "NONE":
                raise ValueError("EMPTY_FACT")
            if "\n" in text or "\r" in text:
                raise ValueError("FACT_MUST_BE_ONE_ATOMIC_LINE")
            if qid not in status:
                raise ValueError("UNKNOWN_QUERY_ID")
            key = (text, tuple(sorted(source_refs)))
            item = valid.setdefault(key, dict(text=text, source_refs=source_refs, routes=[]))
            # Query state is runtime-owned snapshot data. The model chooses only
            # the route; ACTIVE/DORMANT/RESOLVED is attached deterministically.
            route = dict(query_id=qid, observed_status=status[qid])
            if route not in item["routes"]:
                item["routes"].append(route)
        except (ValueError, TypeError) as exc:
            rejected.append({"item": row, "error": str(exc)})
    return UpdateResponseR2(context_id=context["context_id"], facts=[Observation(**f) for f in valid.values()], hints=hints), rejected


class RepairScope:
    """Frozen claims/routes: bad repair output cannot authorize a fresh extraction."""

    def __init__(self, rejected, retained):
        self.targets = []
        self.retained = set()
        self.remember(retained)
        for index, r in enumerate(rejected, start=1):
            f = fields(r["item"])
            if f and f[0] == "PLAN_HINT" and len(f) in {2, 3}:
                self.targets.append({"repair_id": f"R{index}", "kind": "PLAN_HINT", "text": f[-1]})
            elif len(f) in {2, 3} and f[-1] not in {"", "NONE"}:
                self.targets.append({"repair_id": f"R{index}", "kind": "FACT",
                                     "query_id": f[0], "text": f[-1]})

    def remember(self, update):
        for f in update.facts:
            for r in f.routes:
                identity = (r.query_id, f.text, tuple(sorted(f.source_refs)))
                self.retained.add(identity)
        for h in update.hints:
            self.retained.add(("PLAN_HINT", h.text, tuple(sorted(h.source_refs))))

    def restrict(self, update):
        kept, hints, errors = [], [], []
        for f in [*update.facts, *update.hints]:
            routes = f.routes if isinstance(f, Observation) else [None]
            for route in routes:
                qid = route.query_id if route else "PLAN_HINT"
                identity = (qid, f.text, tuple(sorted(f.source_refs)))
                if identity in self.retained:
                    continue  # Harmless identical replay; never new authority.
                target = next((target for target in self.targets
                                if target["text"] == f.text
                                and (target["kind"] == "PLAN_HINT") == (qid == "PLAN_HINT")), None)
                if target is not None:
                    self.targets.remove(target)
                    if route:
                        kept.append(Observation(text=f.text, source_refs=f.source_refs, routes=[route]))
                    else:
                        hints.append(f)
                    continue
                errors.append({"item": f.text, "error": "UPDATE_REPAIR_OUT_OF_SCOPE"})
        result = UpdateResponseR2(context_id=update.context_id, facts=kept, hints=hints)
        self.remember(result)
        return result, errors
