from dataclasses import asdict
from delaybind_core.mwr.experiments import split_question,retention_replay,ablations
from delaybind_core.mwr.config import MWRConfig
from .test_demands_retention import planned,rec
from .test_access_contract import CharacterTokenizer


def test_m63_m66_same_stream_budget_one_factor_and_all_fit():
    base=MWRConfig()
    for name,cfg in ablations(base).items():
        changed=[k for k,v in asdict(base).items() if asdict(cfg)[k]!=v]
        assert len(changed)==(0 if name=='full' else 1)
    report=retention_replay(planned().plan.model_dump(),[[rec(1).model_dump()],[rec(2).model_dump()]],CharacterTokenizer(),[20000])
    assert all(len(r['retained_ids'])==r['window'] for r in report['rows'])
    assert all(r['live_state_tokens']<=r['capacity'] for r in report['rows'])
    assert split_question('original-q')==split_question('original-q')
