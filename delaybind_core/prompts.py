"""Versioned prompt builders for the four stateless model interfaces."""

from __future__ import annotations

import json
from typing import Any

from .schema import AnswerResponse, QueryPlan, UpdateResponse, VerifyResult


PROMPT_VERSION = "v1"


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
        "Declare an answer_contract with a target variable for ENTITY/NUMBER/DATE/SHORT_TEXT "
        "answers (BOOLEAN may use a typed operator result)."
        if require_answer_target
        else "Do not generate answer_contract or an answer target. The Runtime derives answer type "
        "from the Question and a later evidence-grounded ANSWER interface selects the final value."
    )
    return f"""<PLAN version={PROMPT_VERSION}>
Generate a typed QueryPlan for the question. Do not answer the question and do
not use any document, evidence, or gold annotation. RelationSpec descriptions
may be open-ended, but operators must be finite and typed.
Use exact entity mentions from the question as constants. Never invent values
such as unknown_person_1; use variables such as ?director for unknowns.
Every pattern component must connect to an entity/value mentioned in the
question. Preserve semantic relation direction: if the question asks for the
mother of a director, use (?director, mother, ?mother), not the reverse.
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
the QueryPlan. Output events in source order. Do not invent source_ref values;
use the IDs shown in the window. Unrelated windows must return an empty events
array. Proposed actions are suggestions; the deterministic Runtime decides.

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
Determine whether the raw neighborhood supports this exact candidate claim.
Use NEED_MORE_CONTEXT when the provided sentences are insufficient. Do not
infer facts from outside the supplied neighborhood. Accept clear paraphrases
and standard relational implications present in the text (for example, "X is
the son of A and B" supports that B is X's mother when the roles are clear).
Use NEED_MORE_CONTEXT only when a referent is unresolved or the supplied span
is genuinely incomplete, not merely because the relation uses different words.

Question:
{question}

Candidate claim:
{_json(claim)}

Raw neighborhood:
{_json(neighborhood)}

Return only JSON matching this schema:
{_json(schema)}
</VERIFY>"""


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
pack. Return every source_ref actually used; each must be copied exactly from
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
