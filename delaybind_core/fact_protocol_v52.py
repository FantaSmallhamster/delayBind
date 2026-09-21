"""V5.1-style UPDATE lines; independently valid entries survive repair."""

from pydantic import Field

from .schema_v52 import Strict
from .source_refs_v52 import SentenceRefResolver


class FactObservation(Strict):
    query_ids: list[str] = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)
    text: str = Field(min_length=1)


class PlanHint(Strict):
    source_refs: list[str] = Field(min_length=1)
    text: str = Field(min_length=1)


class UpdateV52(Strict):
    facts: list[FactObservation]
    hints: list[PlanHint]


class RecallSelection(Strict):
    selected_fact_ids: list[str]


class UpdateRepairScope:
    """One replacement per original failed item, never a new extraction pass.

    Repair can fix routing, references and syntax, but not rewrite the claim.
    Semantic corrections belong to raw-backed MEMORY/CORRECT. Unrecoverable
    malformed claims must be omitted rather than guessed. Keep this scope across
    retries: a rejected repair response must never authorize its own new facts.
    """

    def __init__(self, rejected):
        from .text_protocol_v52 import fields
        self.targets = []
        for index, rejected_item in enumerate(rejected, 1):
            item = rejected_item.get("item")
            kind, text = None, None
            if isinstance(item, dict) and rejected_item.get("kind") in {"facts", "hints"}:
                kind, text = rejected_item["kind"], item.get("text")
            elif isinstance(item, str):
                f = fields(item)
                if len(f) == 4:
                    kind, text = "facts", f[2]
                elif len(f) == 3 and f[0] == "PLAN_HINT":
                    kind, text = "hints", f[2]
                elif len(f) == 3 and f[-1] in {"ACTIVE", "DORMANT"}:
                    # Common malformed response: missing query-ID column.
                    kind, text = "facts", f[1]
            if isinstance(text, str) and text.strip() and text.strip() != "NONE":
                self.targets.append(dict(repair_id=f"R{index}", kind=kind, text=text.strip()))

    def restrict(self, update):
        accepted, rejected = {"facts": [], "hints": []}, []
        for kind in accepted:
            for item in getattr(update, kind):
                target = next((t for t in self.targets if t["kind"] == kind and t["text"] == item.text.strip()), None)
                if target is None:
                    rejected.append({"kind": kind, "item": item.model_dump(), "error": "UPDATE_REPAIR_OUT_OF_SCOPE"})
                else:
                    self.targets.remove(target)
                    accepted[kind].append(item)
        return UpdateV52(**accepted), rejected


def parse_update(raw: str, *, query_ids, visible_sources):
    from .text_protocol_v52 import fields, lines, refs
    value = {"facts": [], "hints": []}
    rejected_lines = []
    for line in lines(raw):
        if line == "NONE" and len(lines(raw)) == 1:
            continue
        f = fields(line)
        if len(f) == 3 and f[0] == "PLAN_HINT":
            value["hints"].append(dict(source_refs=refs(f[1]), text=f[2]))
        elif len(f) == 4 and f[3] in {"ACTIVE", "DORMANT"}:
            value["facts"].append(dict(query_ids=refs(f[0]), source_refs=refs(f[1]), text=f[2]))
        else:
            rejected_lines.append({"item": line, "error": "Expected query_id | source_refs | fact | ACTIVE or DORMANT"})
    if not raw.strip():
        raise ValueError("Empty UPDATE; use NONE explicitly")
    if not isinstance(value, dict) or set(value) != {"facts", "hints"}:
        raise ValueError("INVALID_UPDATE_ENVELOPE")
    if not all(isinstance(value[k], list) for k in value):
        raise ValueError("INVALID_UPDATE_ARRAYS")
    valid, rejected = {"facts": [], "hints": []}, rejected_lines
    resolver = SentenceRefResolver(visible_sources)
    for kind in valid:
        for item in value[kind]:
            try:
                parsed = (FactObservation if kind == "facts" else PlanHint).model_validate(item)
                resolver.resolve(parsed.source_refs)
                if kind == "facts" and not set(parsed.query_ids) <= set(query_ids):
                    raise ValueError("UNKNOWN_QUERY_ID")
                valid[kind].append(parsed)
            except (ValueError, TypeError) as exc:
                rejected.append({"kind": kind, "item": item, "error": str(exc)})
    return UpdateV52(**valid), rejected
