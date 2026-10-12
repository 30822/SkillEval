"""TRAIN extraction: every extraction pass on every pair; supported observations become skills."""
import json
from pathlib import Path
from common.config import ROOT
from common.pipeline import contracts
from common.pipeline.data import load_pairs
from common.pipeline.llm import model_settings, run_jobs, write_json
from common.pipeline.stage import client, open_run, output_dir, prompts, settings, sha256
from common.skill_schema import SCHEMA_VERSION, TASKS, build_catalog, load_bank, validate


def skill_from(observation, dataset, spec, pair, pass_name, number, model):
    return {'id': f'{dataset}/{pair["id"]}/{pass_name}/{number}', 'domain': spec['domain'], 'task': spec['task'],
            'name': observation['name'], 'action': observation['action'],
            'applicability': {**observation['applicability'], 'description': None, 'context': {}},
            'preserve': observation['preserve'],
            'evaluation': {'attempt_definition': None, 'pass_definition': None, 'fail_definition': None, 'notes': None, 'eligible': None},
            'evidence': {'records': [{'pair_id': pair['id'], **observation['evidence'], 'rationale': observation['rationale']}], 'ids': []},
            'provenance': {'stage': 'extract', 'pass': pass_name, 'model': model},
            'extensions': {}}


def run(config, dataset, *, execute=False, limit=None, output=None, model=None, workers=None):
    spec = config['datasets'][dataset]
    values = settings(config, dataset, {'model': model, 'workers': workers})
    stage_prompts = prompts(spec)['extract']
    excluded = []
    pairs = load_pairs(dataset, 'train', limit, excluded)
    folder = output_dir(spec, 'extraction', output)
    manifest = {'dataset': dataset, 'stage': 'extract', 'model': model_settings(config, values['model']),
                'max_output_tokens': values['max_output_tokens'], 'train_pairs': len(pairs), 'excluded': excluded,
                'passes': {name: sha256(text.encode()) for name, text in stage_prompts.items()}}
    if not execute:
        name = next(iter(stage_prompts))
        print(json.dumps({'output': str(folder), **manifest}, ensure_ascii=False, indent=2))
        print(f'\n--- {name} system prompt ---\n{stage_prompts[name]}\n\n--- first user payload ---')
        print(json.dumps({'pair_id': pairs[0]['id'], 'source': pairs[0]['source'], 'human': pairs[0]['human']}, ensure_ascii=False, indent=2))
        return None
    open_run(folder, manifest)
    api = client(config, values, folder)
    schema = contracts.extract_schema()

    def job(pair, name):
        payload = {'pair_id': pair['id'], 'source': pair['source'], 'human': pair['human']}
        return api.call(stage_prompts[name], payload, 'observations', schema, lambda r: contracts.validate_extract(r, pair))

    jobs = {(pair['index'], name): (lambda p=pair, n=name: job(p, n)) for pair in pairs for name in stage_prompts}
    results, errors = run_jobs(jobs, values['workers'])
    observations, skills = [], []
    for pair in pairs:
        for name in stage_prompts:
            if (pair['index'], name) not in results:
                continue
            for number, row in enumerate(results[(pair['index'], name)]['observations'], 1):
                observations.append({'pair_id': pair['id'], 'pass': name, 'number': number, **row})
                if row['quality'] == 'supported':
                    skills.append(skill_from(row, dataset, spec, pair, name, number, manifest['model']['id']))
    bank = validate({'schema_version': SCHEMA_VERSION, 'domain': spec['domain'], 'task': spec['task'], 'skills': skills,
                     'metadata': {'native_item_key': None, 'native_keys': [], 'native_metadata': {}, 'source_snapshot': None}})
    failures = [{'index': index, 'pass': name, 'error': error} for (index, name), error in sorted(errors.items())]
    write_json(folder / 'observations.json', observations)
    write_json(folder / 'skills.json', bank)
    write_json(folder / 'failures.json', failures)
    summary = {'completed_calls': len(results), 'failed_calls': len(failures), 'observations': len(observations),
               'supported_skills': len(skills)}
    write_json(folder / 'summary.json', summary)
    print(json.dumps({'output': str(folder), **summary}, indent=2))
    return summary


def build_skills(config, domain, *, output=None, sources=None):
    """Merge the three task extraction outputs of a domain into one skills/<domain>.json-style catalog."""
    specs = {spec['task']: spec for spec in config['datasets'].values() if spec['domain'] == domain}
    sources = sources or {task: output_dir(specs[task], 'extraction', None) / 'skills.json' for task in TASKS}
    catalog = build_catalog(domain, {task: load_bank(path) for task, path in sources.items()})
    target = Path(output) if output else ROOT / 'runs' / domain / 'pipeline' / 'skills.json'
    write_json(target, catalog)
    print(json.dumps({'output': str(target), 'skill_count': catalog['skill_count'],
                      'tasks': {task: topic['skill_count'] for task, topic in catalog['topics'].items()}}, indent=2))
    return catalog
