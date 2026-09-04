"""Versioned prompt builders for the four stateless model interfaces."""

from __future__ import annotations

import json
from typing import Any

from .schema import AnswerResponse, QueryPlan, UpdateResponse, VerifyDecision


PROMPT_VERSION = "v2"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def plan_prompt(
    question: str,
    *,
    schema: dict[str, Any],
    correction: str | None = None,
    require_answer_target: bool = True,
) -> str:
    correction_block = ""
    if correction:
        correction_block = f"""
The previous plan failed deterministic validation. Correct these issues and
return a complete replacement plan:
{correction}
"""
    answer_instruction = (
        "Declare an answer_contract with a target variable for ENTITY/COUNTRY/NATIONALITY/"
        "LOCATION/NUMBER/DATE/SHORT_TEXT "
        "answers (BOOLEAN may use a typed operator result)."
        if require_answer_target
        else "Do not generate answer_contract or an answer target. The Runtime derives answer type "
        "from the Question and a later evidence-grounded ANSWER interface selects the final value."
    )
    return f"""<PLAN version={PROMPT_VERSION}>
Generate a typed QueryPlan that compiles into an Open Query Graph for the
question. Do not answer the question and do not use any document, evidence, or
gold annotation. Each Pattern is one directed query edge; equal variable names
are shared variable nodes, concrete entities are constant nodes, and operators
are graph operator nodes. RelationSpec descriptions may be open-ended, but
operators must be finite and typed.
Use exact entity mentions from the question as constants. Never invent values
such as unknown_person_1; use variables such as ?director for unknowns.
Every pattern component must connect to an entity/value mentioned in the
question. Preserve semantic relation direction: if the question asks for the
mother of a director, use (?director, mother, ?mother), not the reverse.
When a question uses an alternate name for a concrete entity, record only a
known, explicit alternate surface form in that pattern's qualifiers as
subject_aliases or object_aliases. Never guess aliases or use aliases for
variables.
Provide one RelationSpec for each relation family when possible. {answer_instruction}
Return all required hops, not a free-form reasoning trace. Safe operator names are DIRECT, PATH_JOIN,
REGISTERED_RULE, RULE, COMPARE, EARLIER, LATER, YOUNGER, OLDER, COUNT,
INTERSECTION, UNION, FILTER, ARGMAX, ARGMIN, and PROJECT.

Tuple direction is subject -> relation -> object: for "the director of Film X"
write (Film X, director, ?director); for "the mother of that director" write
(?director, mother, ?mother). The final answer_contract.target would be
?mother. This is a semantics example only; use the exact entity in the current
question and do not copy Film X.

Question:
{question}
{correction_block}

Return only JSON matching this schema:
{_json(schema)}
</PLAN>"""


def update_prompt(
    question: str,
    plan: QueryPlan,
    graph: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    schema: dict[str, Any],
) -> str:
    return f"""<UPDATE version={PROMPT_VERSION}>
Read only the current window. Extract sparse, atomic claims that could satisfy
the active frontier of the Open Query Graph. Every event must be directly
supported by its own source_ref and a contiguous span in that source's text.
Do not combine two sources, infer a missing relation from the QueryPlan, or
turn the question's requested answer into an asserted fact. Each event must
include span_hint copied as a contiguous phrase from its source. Both event
endpoints must occur in that sentence or match its document title. Output
events in source order. Do not invent source_ref values; use the IDs shown in
the window. Unrelated windows must return an empty events array. Proposed
actions are suggestions; the deterministic Runtime decides.

ACTIVE edges have at least one grounded endpoint and are the primary targets.
DORMANT edges have two unbound variables: emit a DEFER candidate only when the
source itself explicitly states the concrete subject-relation-object fact.
Endpoint types are semantic constraints (for example NATIONALITY accepts an
adjectival nationality such as "Chinese"). When an endpoint lists multiple
binding candidates, facts grounded in any listed candidate are relevant; the
Runtime selects a path only after downstream evidence is verified.
For a NATIONALITY object, extract nationality/demonym adjectives directly
attached to the grounded person. If the text explicitly gives a compound
identity with multiple nationality components, emit each component in source
order rather than silently choosing one. For a PERSON relation, the document
title can supply a pronoun's subject: in a document titled A, "He succeeded
his father B" directly supports (A, father, B).
For DATE objects, biographical lead forms such as "A (d. 20 March 851)"
directly express date of death and "A (born 9 February 1976)" expresses date
of birth. For LOCATION objects, "was assassinated/died in X" directly
expresses place of death.
Normalized values may differ from their surface form, but the copied span must
contain the asserted relation. Do not emit a dormant candidate merely because
the relation family appears.

Question:
{question}

QueryPlan:
{_json(plan.model_dump(mode='json'))}

Verified Graph (compact projection):
{_json(graph)}

Current window:
{_json(entries)}

Return only JSON matching this schema:
{_json(schema)}
</UPDATE>"""


def verify_prompt(
    question: str,
    claim: dict[str, Any],
    neighborhood: list[dict[str, Any]],
    *,
    schema: dict[str, Any],
) -> str:
    return f"""<VERIFY version={PROMPT_VERSION}>
Classify only whether the raw neighborhood supports this exact atomic
candidate claim. Do not try to answer the full Question, require another hop,
require another entity, or require a comparison result. For example, if the
claim is "Film A -> director -> Alex", ACCEPT when the source establishes that
relation even if the question later asks for Alex's mother or birth date.

Read a relation edge as "the subject's RELATION is the object". Thus
(A, father, B) means B is A's father; (A, mother, B) means B is A's mother; and
(A, child, B) means B is A's child. Accept direct inverse wording such as
"A is a son of B" for (A, father, B) when gender and roles are clear.
Kinship requires the stated role, not mere co-occurrence. Marriage, spouse,
husband, wife, or "queen by marriage to" never supports mother/father/child;
REJECT such a kinship claim when the supplied text instead establishes a
spousal relation. Likewise, "B is a daughter of A" makes B A's child, not A
B's child or mother.

Use ACCEPT when a supplied sentence directly states the candidate or gives a
clear paraphrase. Use REJECT only when a supplied sentence contradicts this
same atomic relation. Use NEED_MORE_CONTEXT only when this claim itself is
not established by the supplied text or has an unresolved referent. Do not use
outside knowledge.

Return a non-empty reason. For ACCEPT, cite one or more source_ref values from
the supplied neighborhood and copy a contiguous supporting_text span from one
of those cited sources. A relation wording difference alone is not grounds for
NEED_MORE_CONTEXT: for example, "X is the son of A and B" supports that B is
X's mother when the roles are explicit.

Candidate claim:
{_json(claim)}

Raw neighborhood:
{_json(neighborhood)}

Return only JSON matching this schema:
{_json(schema)}
</VERIFY>"""


def targeted_update_prompt(
    question: str,
    edge: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    schema: dict[str, Any],
) -> str:
    return f"""<UPDATE_CALLBACK version={PROMPT_VERSION}>
Perform a high-recall extraction pass for exactly one ACTIVE Open Query Graph
edge. Inspect only the supplied entries. Return every atomic fact that can
satisfy this edge, and nothing for other edges. Do not answer the full question
or require downstream evidence.

Interpret each edge as "the subject's RELATION is the object". In particular,
(A, father, B) means B is A's father, and (A, child, B) means B is A's child.
The relation label need not occur verbatim: extract clear paraphrases, inverse
kinship wording, adjectival nationalities, and other direct semantic
realizations when the supplied source establishes the same edge.

Every event must use a supplied source_ref, both factual endpoints must be
supported by that source, and span_hint must be copied as a contiguous phrase
from the same source. A document title may resolve the subject of a pronoun.
Return an empty events array when none of the entries supports the edge.

The target edge ID is fixed by Runtime. For each match, emit one normal
TripleEvent with a fresh event_id, the supplied source_ref, the concrete
subject/object, the target relation, and copied span_hint. Set pattern_hint to
the supplied edge ID. Do not emit facts for another edge. A concrete endpoint
may match any value in subject_candidates/object_candidates, not only the
currently selected subject/object. Respect subject_type and object_type.
For object_type=NATIONALITY, adjective forms attached to the person are direct
evidence; split an explicit compound nationality into separate events in
source order. For a PERSON edge, resolve "he/she/his/her" to the supplied
document title when the title is the grounded subject.
For object_type=DATE, extract dates from lead-parenthetical abbreviations such
as "d." (date of death) and "b."/"born" (date of birth). For
object_type=LOCATION, death wording such as "assassinated in X" supports place
of death when the target relation is place of death.

Question (for entity disambiguation only):
{question}

Target ACTIVE edge:
{_json(edge)}

Read-prefix entries:
{_json(entries)}

Return only JSON matching this schema:
{_json(schema)}
</UPDATE_CALLBACK>"""


def answer_prompt(
    question: str,
    evidence_pack: dict[str, Any],
    *,
    schema: dict[str, Any],
) -> str:
    return f"""<ANSWER version={PROMPT_VERSION}>
Answer the question using only the verified EvidencePack. Do not access the
raw document or introduce unsupported facts. The graph may contain multiple
intermediate variables: determine the requested value from the Question, not
from a predeclared target variable. You may apply simple reasoning over the
verified claims and operator trace, but never introduce a fact absent from the
pack. When answer_value is non-null and the operator trace marks its producing
operator EXECUTED, treat it as the deterministic graph result and return that
value. Return the minimal canonical entity at the granularity requested by the
question. For a place-of-birth/death question, prefer the city or municipality
over a concatenated neighborhood-plus-city phrase; return a country only when
the question asks for a country. Return every source_ref actually used; each must be copied exactly from
an EvidenceAssertion in the pack. If the pack cannot determine the answer,
return answer=null and an empty source_refs list.

Question:
{question}

EvidencePack:
{_json(evidence_pack)}

Return only JSON matching this schema:
{_json(schema)}
</ANSWER>"""


def direct_prompt(
    question: str,
    entries: list[dict[str, Any]],
    *,
    schema: dict[str, Any],
) -> str:
    """Prompt for the same-checkpoint Direct Full Context baseline."""
    return f"""<DIRECT version={PROMPT_VERSION}>
Answer the question using the complete context below. Do not use outside
knowledge. This is a baseline: do not emit a plan, memory summary, or chain of
thought. Return the answer and, when possible, the supporting source_ref IDs
shown in the context.

Question:
{question}

Complete context:
{_json(entries)}

Return only JSON matching this schema:
{_json(schema)}
</DIRECT>"""
