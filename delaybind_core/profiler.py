"""Question contract and operator profiling for canonical 2Wiki records."""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from .data import CanonicalSample
from .schema import AnswerContract, QuestionContract


def infer_required_ops(sample: CanonicalSample) -> list[str]:
    question_type = (sample.question_type or "").casefold()
    operators: list[str] = []
    if question_type in {"compositional", "bridge", "bridge-composition"}:
        operators.append("PATH_JOIN")
    if question_type == "inference":
        operators.append("REGISTERED_RULE")
    if question_type in {"comparison", "bridge-comparison"}:
        operators.append("COMPARE")
        question = sample.question.casefold()
        if re.search(r"\b(first|earlier|before|oldest|born first)\b", question):
            operators.append("EARLIER")
        elif re.search(r"\b(last|later|after|youngest|younger)\b", question):
            operators.append("LATER")
    # Keep each operator once while preserving deterministic order.
    return list(dict.fromkeys(operators))


def infer_answer_contract(sample: CanonicalSample) -> AnswerContract:
    question = sample.question.casefold()
    if re.search(r"\b(is|are|was|were|did|does|do)\b", question) and question.startswith(("is ", "are ", "was ", "were ", "did ", "does ", "do ")):
        answer_type = "BOOLEAN"
    elif re.search(r"\bhow many\b|\bnumber of\b|\bcount\b", question):
        answer_type = "NUMBER"
    elif re.search(r"\bwhen\b|\bdate\b|\byear\b", question):
        answer_type = "DATE"
    elif re.search(r"\bwho\b|\bwhich (person|film|movie|company)\b", question):
        answer_type = "ENTITY"
    else:
        answer_type = "SHORT_TEXT"
    return AnswerContract(
        target=None,
        type=answer_type,
        cardinality="SINGLE",
        normalization="2WIKI" if answer_type in {"ENTITY", "SHORT_TEXT"} else "IDENTITY",
    )


def profile_sample(sample: CanonicalSample) -> QuestionContract:
    required_ops = infer_required_ops(sample)
    answer_contract = infer_answer_contract(sample)
    evidence_cardinality = "SET" if len(sample.evidences) > 1 else "SINGLE"
    return QuestionContract(
        sample_id=sample.sample_id,
        question_type=sample.question_type,
        required_ops=required_ops,
        answer_contract=answer_contract,
        evidence_cardinality=evidence_cardinality,
        metadata={
            "evidence_count": len(sample.evidences),
            "supporting_fact_count": len(sample.supporting_facts),
            "document_count": len(sample.documents),
        },
    )


def profile_dataset(samples: Iterable[CanonicalSample]) -> dict[str, object]:
    contracts = [profile_sample(sample) for sample in samples]
    type_counts = Counter(contract.question_type for contract in contracts)
    op_counts = Counter(op for contract in contracts for op in contract.required_ops)
    return {
        "sample_count": len(contracts),
        "question_types": dict(sorted(type_counts.items(), key=lambda item: str(item[0]))),
        "operators": dict(sorted(op_counts.items())),
        "contracts": [contract.model_dump(mode="json") for contract in contracts],
    }
