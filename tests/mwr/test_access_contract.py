import asyncio
import json
from dataclasses import replace
import pytest
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.archive import RawArchive
from delaybind_core.evaluation import ExperimentConfig


class CharacterTokenizer:
    fingerprint = "test-only-character-tokenizer-v1"
    def encode(self, text): return list(text)
    def decode(self, values): return "".join(values)


class EmptyClient:
    def __init__(self): self.calls = []
    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs['interface'] == 'UPDATE': return '{"evidence":[]}'
        if kwargs['interface'] == 'PLAN': return '{"queries":[]}'
        return '{"status":"INSUFFICIENT","answer":null,"source_refs":[]}'


def forbidden(*args, **kwargs): raise AssertionError("forbidden archive access")


def guard(monkeypatch):
    for name in ('search_mentions', 'expanded_context', 'read_entries'):
        monkeypatch.setattr(RawArchive, name, forbidden)
    for name in ('search_raw_mentions', 'list_raw_spans', 'fetch_raw_document'):
        monkeypatch.setattr(SQLiteEventStore, name, forbidden)


def test_m01_explicit_engine_and_no_search():
    for flag in ('raw_search_enabled', 'global_reread_enabled', 'answer_gap_search_enabled'):
        with pytest.raises(ValueError): RunnerConfig(engine='delaybind_mwr', **{flag:True})
    with pytest.raises(ValueError): RunnerConfig(engine='accidental_suffix')
    with pytest.raises((TypeError, ValueError)):
        ExperimentConfig.from_mapping({'input':'x','runner':{'engine':'delaybind_mwr','typo':1}})


def test_m02_m04_forward_resume_cache_namespace(monkeypatch):
    guard(monkeypatch)
    client, store = EmptyClient(), SQLiteEventStore()
    cfg = RunnerConfig(engine='delaybind_mwr', chunk_size=3)
    runner = V5Runner(client, config=cfg, tokenizer=CharacterTokenizer())
    kw = dict(run_id='new', question='q', context='abcdefghi', store=store)
    result = asyncio.run(runner.run(**kw))
    assert result['windows_processed'] == 3
    before = len(client.calls)
    assert asyncio.run(runner.run(**kw)) == result
    assert len(client.calls) == before
    changed = V5Runner(client, config=replace(cfg, chunk_size=4), tokenizer=CharacterTokenizer())
    with pytest.raises(ValueError, match='contract changed'): asyncio.run(changed.run(**kw))
    assert all('MWR request contract' in call['messages'][0]['content'] for call in client.calls)


def test_m03_recovery_after_network_error(monkeypatch):
    guard(monkeypatch)
    class Flaky(EmptyClient):
        failed = False
        updates = 0
        async def complete(self, **kwargs):
            if kwargs['interface'] == 'UPDATE':
                self.updates += 1
                if self.updates == 2 and not self.failed:
                    self.failed = True
                    raise ConnectionError('simulated network error')
            return await super().complete(**kwargs)
    client, store = Flaky(), SQLiteEventStore()
    runner = V5Runner(client, config=RunnerConfig(engine='delaybind_mwr', chunk_size=3), tokenizer=CharacterTokenizer())
    kw = dict(run_id='retry', question='q', context='abcdefghi', store=store)
    with pytest.raises(ConnectionError): asyncio.run(runner.run(**kw))
    assert store.latest_snapshot('retry')['payload']['source']['windows'] == 1
    result = asyncio.run(runner.run(**kw))
    assert result['windows_processed'] == 3
    windows = [c['messages'][1]['content'] for c in client.calls if c['interface']=='UPDATE']
    assert sum('abc' in w for w in windows) == 1
