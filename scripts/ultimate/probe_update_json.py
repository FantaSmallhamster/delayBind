"""Small synthetic provider probes; no benchmark questions and no stored keys."""

import getpass
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.update_json import UPDATE_JSON_SCHEMA, parse_update_json


def main():
    out = ROOT / 'reports/ultimate/v06a_update_json/provider_probe.json'
    if out.exists():
        raise SystemExit('Probe already recorded; do not overwrite capability evidence.')
    key = os.environ.get('MODEL_API_KEY') or getpass.getpass('API key (hidden, memory only): ')
    results = []

    def probe(name, response_format, prompt):
        payload = {'model': 'Qwen/Qwen3.5-9B', 'temperature': 0, 'top_p': .95,
                   'seed': 4, 'enable_thinking': False, 'max_tokens': 256,
                   'messages': [{'role': 'user', 'content': prompt}]}
        if response_format is not None:
            payload['response_format'] = response_format
        row = {'name': name, 'request': payload}
        request = Request('https://api.siliconflow.cn/v1/chat/completions',
                          data=json.dumps(payload).encode(), headers={
                              'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=45) as response:
                row['status'] = response.status
                row['response'] = json.loads(response.read())
        except HTTPError as exc:
            row['status'] = exc.code
            row['error'] = exc.read().decode(errors='replace')[:1000]
        except (URLError, TimeoutError) as exc:
            row['status'] = None
            row['error'] = str(exc)
        results.append(row)
        print(name, row['status'], (row.get('response', {}).get('choices') or [{}])[0].get('message', {}).get('content', row.get('error')), flush=True)
        return row

    strict = {'type': 'json_schema', 'json_schema': {'name': 'schema_probe', 'strict': True,
              'schema': {'type': 'object', 'required': ['probe'], 'additionalProperties': False,
                         'properties': {'probe': {'type': 'string', 'enum': ['schema_controlled']}}}}}
    first = probe('strict_schema_conflicting_prompt', strict,
                  'Return JSON with the single field wrong and numeric value 7. Do not include probe.')
    selected = None
    try:
        constrained = json.loads(first['response']['choices'][0]['message']['content']) == {'probe': 'schema_controlled'}
    except (KeyError, IndexError, TypeError, ValueError):
        constrained = False
    fixture = ('Return one JSON object with facts and hints arrays. Each fact has query, sources, text only. '
               'No facts means an empty array. Hints is empty. Q1 asks who directed Fern. '
               'Source S1: Ada directed Fern. Extract the fact for Q1 citing S1. Do not include status or explanations.')
    if constrained:
        second = probe('strict_schema_actual_update_shape', {'type': 'json_schema', 'json_schema': {
            'name': 'update', 'strict': True, 'schema': UPDATE_JSON_SCHEMA}}, fixture)
        try:
            parsed = parse_update_json(second['response']['choices'][0]['message']['content'])
            second['query_id_valid'] = all(f.query_id == 'Q1' for f in parsed.facts)
            if len(parsed.facts) == 1 and not parsed.rejected_lines and parsed.facts[0].source_refs == ['S1']:
                selected = 'json_schema'
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    if selected is None:
        second = probe('json_object_update_shape', {'type': 'json_object'}, fixture)
        try:
            parsed = parse_update_json(second['response']['choices'][0]['message']['content'])
            second['query_id_valid'] = all(f.query_id == 'Q1' for f in parsed.facts)
            if len(parsed.facts) == 1 and not parsed.rejected_lines and parsed.facts[0].source_refs == ['S1']:
                selected = 'json_object'
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    if selected is None:
        third = probe('prompt_only_update_shape', None, fixture)
        try:
            parsed = parse_update_json(third['response']['choices'][0]['message']['content'])
            third['query_id_valid'] = all(f.query_id == 'Q1' for f in parsed.facts)
            if len(parsed.facts) == 1 and not parsed.rejected_lines and parsed.facts[0].source_refs == ['S1']:
                selected = 'prompt'
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    out.write_text(json.dumps({'tested_at': datetime.now(timezone.utc).isoformat(),
                              'selected_mode': selected, 'strict_probe_constrained': constrained,
                              'note': 'Finite live probes, not a guarantee of universal schema compliance.',
                              'probes': results}, ensure_ascii=False, indent=2) + '\n')
    print('SELECTED_MODE', selected, flush=True)
    return 0 if selected else 2


if __name__ == '__main__':
    raise SystemExit(main())
