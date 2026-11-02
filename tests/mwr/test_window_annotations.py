import pytest
from delaybind_core.data import canonicalize_record
from scripts.mwr.bundle_tools import documents
from scripts.mwr.window_annotations import ranges_cover, visibility
from delaybind_core.mwr.common import digest
from .test_access_contract import CharacterTokenizer


def test_source_visibility_joins_fragments_but_not_gaps():
    assert ranges_cover([(0, 4), (4, 9)], 2, 8)
    assert not ranges_cover([(0, 4), (5, 9)], 2, 8)
    assert not ranges_cover([(3, 10)], 2, 8)
    assert ranges_cover([(3, 10), (0, 5)], 2, 8)


def test_support_visibility_is_fixed_before_any_binding_and_rejects_stale_text():
    raw = {'id': 'q', 'input': 'Where was Neri born?', 'context': 'Document 1:\nNeri\nNeri was born in Elm.'}
    sample = canonicalize_record(raw)
    body = documents(raw['context'])[0]['text']
    fact = {'sample_id': 'q', 'mapping_status': 'EXACT', 'doc_id': 'd-' + digest(['', body])[:24],
            'start': 5, 'end': len(body), 'text': body[5:]}
    row = visibility(sample, [fact], CharacterTokenizer(), 10)
    assert row['window_count'] == 3 and row['support_sentences'][0]['first_complete_window'] == 3
    assert 'bridge_complete_window' not in row['support_sentences'][0]
    with pytest.raises(ValueError, match='does not match'):
        visibility(sample, [{**fact, 'text': 'different'}], CharacterTokenizer(), 10)
