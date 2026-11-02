"""One live evidence pool. Routing and assessments never own cue bodies."""
from .common import digest, dump, ProtocolError
from .models import EvidenceRecord, Update


def stage_proposals(update, state, access, tokenizer, config, log, observed_ids=None):
    staged = {}
    query_ids = {q.id for q in state.plan.queries}
    observed_ids = observed_ids if observed_ids is not None else set()
    for p in update.evidence[:config.max_proposals]:
        try:
            spans = access.proposal_spans(p.source_refs)
        except ProtocolError as exc:
            log("EVIDENCE_REJECTED", {"reason": str(exc)})
            continue
        valid = sorted(set(p.candidate_query_ids) & query_ids)
        if set(p.candidate_query_ids) - query_ids:
            log("ROUTING_WARNING", {"unknown_query_ids": sorted(set(p.candidate_query_ids) - query_ids)})
        if not valid and not config.allow_unrouted:
            continue
        eid = "e-" + digest([p.cue_text.strip(), sorted(s.model_dump_json() for s in spans)])[:24]
        record = EvidenceRecord(evidence_id=eid, source_spans=spans, cue_text=p.cue_text.strip(),
            entity_mentions=p.entity_mentions, relation_cues=p.relation_cues,
            candidate_query_ids=valid, observed_window=access.windows)
        previous = staged.get(eid) or state.evidence.get(eid)
        if previous:
            record = previous.model_copy(update={
                "candidate_query_ids": sorted(set(previous.candidate_query_ids) | set(valid)),
                "entity_mentions": sorted(set(previous.entity_mentions) | set(record.entity_mentions)),
                "relation_cues": sorted(set(previous.relation_cues) | set(record.relation_cues))})
        tentative = {**staged, eid: record}
        if len(tokenizer.encode(dump({"evidence":{k:v.model_dump() for k,v in tentative.items()},"plan_notes":[]}))) > config.max_staging_tokens:
            log("BUDGET_BLOCKED", {"reason": "staging_tokens", "evidence_id": eid})
            continue
        if eid in observed_ids and eid not in state.evidence:
            log("REOBSERVED", {"evidence_id": eid, "window": access.windows})
        observed_ids.add(eid)
        staged[eid] = record
        log("EVIDENCE_PROPOSED", {"evidence": record.model_dump(), "state_version": state.version})
    if len(update.evidence) > config.max_proposals:
        log("BUDGET_BLOCKED", {"reason": "proposal_count", "discarded": len(update.evidence)-config.max_proposals})
    return staged


def migrate_legacy_update(facts, hints):
    """Explicit data adapter; ambiguous hints stay audit-only, never acquire a pointer."""
    evidence, notes, audit = [], [], []
    for item in facts:
        refs = item.get('source_refs') or ([item['source_ref']] if item.get('source_ref') else [])
        if refs and item.get('text'):
            evidence.append({'source_refs':refs, 'cue_text':item['text'],
                             'candidate_query_ids':[item['query_id']] if item.get('query_id') else []})
    for hint in hints:
        # Classification must be explicit. A free-form mixed legacy hint is not evidence.
        if isinstance(hint, dict) and hint.get('kind') == 'source_cue' and hint.get('source_refs') and hint.get('text'):
            evidence.append({'source_refs':hint['source_refs'], 'cue_text':hint['text']})
        elif isinstance(hint, dict) and hint.get('kind') == 'plan_note':
            notes.append(str(hint.get('text','')))
        else:
            audit.append({'classification':'UNCLASSIFIED', 'original':hint})
    return Update.model_validate({'evidence':evidence,'plan_notes':notes}), audit
