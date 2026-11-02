"""Bounded in-memory candidate selection; never grants semantic support."""
from .common import dump, digest
from .retention import words


def select_candidates(demand, pool, tokenizer, config):
    rel=words(' '.join(demand['relation_cues'])+' '+demand['rendered_query'])
    known=words(' '.join(str(v) for v in demand['known_inputs'].values()))
    candidates=[]
    for e in pool.values():
        query_match=demand['id'] in e.candidate_query_ids
        relation_match=bool(rel & words(' '.join(e.relation_cues)+' '+e.cue_text))
        entity_match=bool(known & words(' '.join(e.entity_mentions)+' '+e.cue_text))
        if query_match or relation_match or entity_match:
            # Unknown/ambiguous aliases are not hard exclusions. Entity matches
            # include potentially contradictory records, not only positive cues.
            score=(int(entity_match),int(relation_match),int(query_match))
            candidates.append((score,e))
    candidates.sort(key=lambda item:(tuple(-x for x in item[0]),item[1].observed_window,item[1].evidence_id))
    selected=[]
    for _,e in candidates:
        if len(selected)>=config.max_candidates: break
        trial=selected+[e]
        if len(tokenizer.encode(dump([v.model_dump() for v in trial])))<=config.max_candidate_tokens:
            selected=trial
    return selected, {'pool_size':len(candidates),'presented':len(selected),
        'unreviewed':len(candidates)-len(selected),
        'candidate_version':digest([e.model_dump() for _,e in candidates])}
