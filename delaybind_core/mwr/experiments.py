"""Offline fixed-proposal retention ablations and group-safe split utilities."""
from dataclasses import replace
from .models import LiveState,EvidenceRecord,Plan
from .retention import retain,tokens
from .config import MWRConfig
from .common import digest


def split_question(original_question_id,seed=4):
    value=int(digest([seed,original_question_id])[:8],16)%100
    return 'train' if value<70 else ('dev' if value<85 else 'test')


def retention_replay(plan,windows,tokenizer,budgets=(2000,4000,8000)):
    """Inputs are one frozen predicted plan/proposal stream, with no gold decisions."""
    rows=[]
    for capacity in budgets:
        for policy in ('fifo','current_only','uniform','demand'):
            state=LiveState(plan=Plan.model_validate(plan))
            cfg=MWRConfig(max_live_state_tokens=capacity,retention_policy=policy)
            for window,records in enumerate(windows,1):
                for record in records:
                    e=EvidenceRecord.model_validate(record)
                    state.evidence[e.evidence_id]=e
                state.version+=1
                state=retain(state,tokenizer,cfg,lambda *x:None)
                rows.append({'policy':policy,'capacity':capacity,'window':window,
                    'retained_ids':sorted(state.evidence),'live_state_tokens':tokens(state,tokenizer)})
    return {'schema_version':'mwr-retention-replay-v1','proposal_stream_hash':digest([plan,windows]),
        'disclosure':'Shared proposal stream: measures retention only, not extraction benefit. No binding/solver outcomes implied.',
        'rows':rows}


def ablations(base):
    """Exactly one mechanism changes per derived configuration."""
    return {
        'full':base,
        'without_dormant_reading':replace(base,show_dormant_demands=False),
        'without_dormant_budget':replace(base,dormant_fraction=0.0),
        'without_unrouted_channel':replace(base,allow_unrouted=False),
        'without_dependency_recall':replace(base,dependency_recall=False),
        'with_relation_hints':replace(base,include_relation_hints=True),
    }
