import asyncio,json
from delaybind_core.evaluation import ExperimentConfig,run_experiment
from .test_engine_flow import StoryClient
from .test_access_contract import CharacterTokenizer


def test_mwr_harness_all_conditions_and_resumes(tmp_path):
    data=tmp_path/'data.json'
    data.write_text(json.dumps([{'id':'q','question':'Where was the director of Film A born?',
        'answer':'Haizhou','context':[['Chen',['Chen was born in Haizhou.']],['Film A',['Film A was directed by Chen.']]]}]))
    config=ExperimentConfig.from_mapping({'input':str(data),'output_dir':str(tmp_path/'run'),
        'methods':['delaybind_mwr','flat_memory_mwr','direct_mwr'],'orders':['original','reverse'],
        'runner':{'engine':'delaybind_mwr','mwr':{'max_live_state_tokens':6000}},'sample_count':1})
    summary=asyncio.run(run_experiment(config,client_factory=lambda s:StoryClient(),tokenizer=CharacterTokenizer()))
    assert summary['count']==6
    rows=[json.loads(s) for s in (tmp_path/'run/results.jsonl').read_text().splitlines()]
    assert all(r['answer_exact'] and r['status']=='ANSWERED' and r['run_success'] for r in rows)
    assert asyncio.run(run_experiment(config,client_factory=lambda s:StoryClient(),tokenizer=CharacterTokenizer()))['count']==6
