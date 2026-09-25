"""Actionable fact-only MEMORY repair guidance, without repairing decisions.

Hints describe syntax only. They never commit a binding, validate entailment,
or broaden the fact IDs authorized by the frozen request.
"""

from .protocol_r2 import fact_alias_map
from .schema_r2 import UNIFIED_MEMORY_INTERFACE
from .text_protocol_v52 import fields


def fact_only_repair_hint(payload):
    """Explain common rejected forms while preserving the original error."""
    error = str(payload["validation_errors"])
    original = payload["original_memory_request"]
    if original.get("memory_interface") == UNIFIED_MEMORY_INTERFACE:
        return unified_memory_repair_hint(payload)
    aliases = fact_alias_map(original["allowed_fact_ids"])
    allowed = set(aliases) | set(aliases.values())
    hints = []
    reserved = {"BOUND", "NOOP", "NONE", "UNBOUND", "REVIEW", "CORRECTION", "CONTEXT"}
    for index, line in enumerate(payload["rejected_response"].splitlines(), 1):
        if not line.strip():
            continue
        parts = fields(line.strip())
        if parts[0] == "BOUND" and len(parts) != 3:
            hints.append(
                f"Line {index}: BOUND has {len(parts)} fields; exactly 3 are required: "
                "BOUND | concrete value | supporting fact IDs. "
                "Supply missing fields or remove extra fields; do not invent evidence."
            )
        elif len(parts) == 2 and parts[0] and parts[0] not in reserved:
            supports = [item.strip() for item in parts[1].split(",")]
            if supports and all(item in allowed for item in supports):
                hints.append(
                    f"Line {index}: received 2 fields (value and supporting fact IDs); "
                    "the required leading BOUND field is missing. If this is your "
                    "supported result, return BOUND | concrete value | supporting fact IDs."
                )
    if "UNKNOWN_FACT_ALIAS" in error:
        hints.append("Unknown support ID: use only IDs displayed in this request's Eligible facts; "
                     "do not copy an ID from an example or another request.")
    if "DUPLICATE_SUPPORT_FACT" in error:
        hints.append("List each supporting fact ID only once within a result line.")
    if any(code in error for code in ("MISSING_MEMBER_PROOF", "MISSING_BINDING_PROOF",
                                      "INFERRED_ONLY_FOR_INTERMEDIATE_QUERY")):
        hints.append("This result cannot omit direct supporting facts in the current context. "
                     "Replace the NONE support field with the actual displayed supporting IDs; "
                     "if no supplied evidence supports the value, do not bind it.")
    if "MEMBER_REQUIRES_ONE_CONCRETE_VALUE" in error:
        hints.append("Each BOUND line needs one concrete result, not an array, unknown variable, "
                     "or placeholder. Use separate BOUND lines for independently supported members.")
    if "MEMBER_MEMORY_EXPECTS_BOUND_LINES_OR_ONE_NOOP" in error or "CANNOT_BE_MIXED" in error:
        hints.append("Do not combine a no-change command with BOUND lines. Return supported "
                     "BOUND lines, or NOOP as the entire reply when no change is justified.")
    if not hints:
        hints.append("Each supported result must have exactly 3 fields: "
                     "BOUND | concrete value | supporting fact IDs. "
                     "For no justified binding change, return only NOOP.")
    hints.append("The rejected reply has not changed runtime state. Return the complete replacement "
                 "for the same branch and evidence snapshot, with no explanation. "
                 "A format error does not by itself justify changing a supported answer to NOOP.")
    return "\n".join(hints)


def unified_memory_repair_hint(payload):
    """Repair the whole neutral result set using the same frozen request."""
    error = str(payload["validation_errors"])
    original = payload["original_memory_request"]
    query_id = original["query_instance"]["id"]
    hints = [f"Use exactly three fields on every line: {query_id} | supporting fact IDs | answer.",
             f"Use only {query_id} and the short fact IDs displayed in Facts."]
    if "FIELD_COUNT" in error:
        hints.append("Supply missing fields or remove extra fields. Escape a literal pipe inside an answer as \\|.")
    if "UNKNOWN_FACT_ALIAS" in error:
        hints.append("Do not copy IDs from another request or invent evidence; cite only displayed short IDs.")
    if "DUPLICATE_SUPPORT_FACT" in error:
        hints.append("List each supporting fact ID only once within a result line.")
    if "MISSING_FACT_SUPPORT" in error:
        hints.append("A concrete answer in this request must cite its supporting facts; NONE is not authorized for it.")
    if "ONE_CONCRETE_ANSWER" in error:
        hints.append("Return one concrete answer per line, with no JSON arrays, placeholders, or joined answer lists.")
    hints.append(f"When the supplied evidence supports no answer, return only {query_id} | NONE | UNKNOWN. "
                 "Do not mix UNKNOWN with supported answers. NONE with a concrete answer is allowed only "
                 "when the supplied Inputs alone determine it.")
    hints.append("The rejected reply has not changed runtime state. Return the complete replacement answer set "
                 "for the same branch and evidence snapshot, with no explanation. A format error does not "
                 "by itself justify changing a supported answer to UNKNOWN.")
    return "\n".join(hints)
