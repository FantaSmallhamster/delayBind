"""Retaining an all-fit pool must not re-tokenize the full state for each log row."""
import pytest
from delaybind_core.mwr.config import MWRConfig
from delaybind_core.mwr.models import Binding
from delaybind_core.mwr.retention import retain, tokens
from .test_access_contract import CharacterTokenizer
from .test_demands_retention import planned, rec


class CountingTokenizer(CharacterTokenizer):
    def __init__(self):
        self.calls = 0

    def encode(self, text):
        self.calls += 1
        return super().encode(text)


@pytest.mark.parametrize('size', [8, 32, 64])
def test_all_fit_tokenization_cost_is_constant_and_audit_is_unchanged(size):
    state = planned()
    state.evidence = {str(i): rec(i) for i in range(size)}
    expected = state.model_copy(deep=True)
    cost = tokens(state, CharacterTokenizer())
    tokenizer = CountingTokenizer()
    events = []
    result = retain(state, tokenizer, MWRConfig(max_live_state_tokens=100000),
                    lambda kind, payload: events.append((kind, payload)))
    assert result == expected
    assert tokenizer.calls <= 3
    assert events == [('EVIDENCE_RETAINED', {'evidence_id': eid, 'reason': 'all_fit',
                                          'before_tokens': cost, 'after_tokens': cost})
                      for eid in state.evidence]


def test_all_fit_logs_cost_after_removing_broken_proof():
    state = planned()
    state.evidence = {'1': rec(1)}
    state.bindings['Q1'] = Binding(query_id='Q1', input_fingerprint='obsolete', value='old value',
                                  support_ids=['missing'], version=1)
    before = tokens(state, CharacterTokenizer())
    events = []
    out = retain(state, CharacterTokenizer(), MWRConfig(max_live_state_tokens=100000),
                 lambda kind, payload: events.append((kind, payload)))
    after = tokens(out, CharacterTokenizer())
    assert not out.bindings and after < before
    kept = [payload for kind, payload in events if kind == 'EVIDENCE_RETAINED']
    assert kept == [{'evidence_id': '1', 'reason': 'all_fit', 'before_tokens': before, 'after_tokens': after}]
