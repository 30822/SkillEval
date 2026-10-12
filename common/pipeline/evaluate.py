"""TEST evaluation: match the bank to each source, dedupe, judge both outputs, compare pass counts."""
import json
from pathlib import Path
from common.config import ROOT
from common.pipeline import contracts
from common.pipeline.data import load_pairs
from common.pipeline.llm import model_settings, read_json, run_jobs, write_json
from common.pipeline.scoring import TARGETS, aggregate, score_pair
from common.pipeline.stage import batches, client, open_run, output_dir, prompts, settings, sha256
from common.skill_schema import load_skills


def build_checklist(skill_ids, matches, decisions):
    """Applicable skills in bank order, minus those deduplicated into a kept representative."""
    applicable = [sid for sid in skill_ids if matches[sid]['verdict'] == 'applicable']
    if decisions is None:
        return applicable
    return [sid for sid in applicable if decisions[sid]['decision'] == 'keep']


def run(config, dataset, *, execute=False, limit=None, output=None, model=None, workers=None, skills=None):
    spec = config['datasets'][dataset]
    values = settings(config, dataset, {'model': model, 'workers': workers})
    stage_prompts = prompts(spec)
    bank_path = Path(skills) if skills else ROOT / spec['skills']
    bank = load_skills(bank_path, spec['domain'], spec['task'])
    views = {skill['id']: contracts.skill_view(skill) for skill in bank['skills']}
    skill_ids = list(views)
    pairs = load_pairs(dataset, 'test', limit or values['evaluation_pairs'])
    by_index = {pair['index']: pair for pair in pairs}
    folder = output_dir(spec, 'evaluation', output)
    manifest = {'dataset': dataset, 'stage': 'evaluate', 'model': model_settings(config, values['model']),
                'max_output_tokens': values['max_output_tokens'], 'bank': str(bank_path), 'bank_sha256': sha256(bank),
                'skills': len(skill_ids), 'test_pairs': len(pairs), 'match_batch_size': values['match_batch_size'],
                'judge_batch_size': values['judge_batch_size'],
                'prompts_sha256': {name: sha256(stage_prompts[name].encode()) for name in ('match', 'dedupe', 'judge')}}
    if not execute:
        print(json.dumps({'output': str(folder), **manifest}, ensure_ascii=False, indent=2))
        for name in ('match', 'dedupe', 'judge'):
            print(f'\n--- {name} system prompt ---\n{stage_prompts[name]}')
        return None
    open_run(folder, manifest)
    api = client(config, values, folder)
    failures = {}

    def phase(jobs):
        results, errors = run_jobs(jobs, values['workers'])
        for key, error in errors.items():
            failures.setdefault(key[0], []).append({'step': key[1:], 'error': error})
        return results

    # 1. Source-only applicability of every bank skill.
    def match(pair, ids):
        payload = {'source': pair['source'], 'skills': [views[sid] for sid in ids]}
        return api.call(stage_prompts['match'], payload, 'matches', contracts.match_schema(ids),
                        lambda r: contracts.validate_match(r, pair['source'], ids))

    groups = batches(skill_ids, values['match_batch_size'])
    results = phase({(p['index'], 'match', g): (lambda p=p, ids=ids: match(p, ids)) for p in pairs for g, ids in enumerate(groups)})
    matches = {}
    for pair in pairs:
        if pair['index'] not in failures:
            matches[pair['index']] = {row['skill_id']: row for g in range(len(groups)) for row in results[(pair['index'], 'match', g)]['matches']}

    # 2. Remove skills that duplicate a kept skill on the same source opportunity.
    def dedupe(pair, ids):
        rows = matches[pair['index']]
        payload = {'source': pair['source'], 'skills': [{**views[sid], 'match_reason': rows[sid]['reason'], 'source_quote': rows[sid]['source_quote']} for sid in ids]}
        return api.call(stage_prompts['dedupe'], payload, 'decisions', contracts.dedupe_schema(ids),
                        lambda r: contracts.validate_dedupe(r, ids))

    applicable = {i: build_checklist(skill_ids, rows, None) for i, rows in matches.items()}
    results = phase({(i, 'dedupe'): (lambda p=by_index[i], ids=ids: dedupe(p, ids)) for i, ids in applicable.items() if len(ids) > 1})
    checklists = {}
    for i, rows in matches.items():
        if i in failures:
            continue
        decisions = {row['skill_id']: row for row in results[(i, 'dedupe')]['decisions']} if (i, 'dedupe') in results else None
        checklists[i] = {'pair_id': by_index[i]['id'], 'matches': [rows[sid] for sid in skill_ids],
                         'decisions': [decisions[sid] for sid in applicable[i]] if decisions else [],
                         'checklist': build_checklist(skill_ids, rows, decisions)}
        write_json(folder / 'checklists' / f'{i}.json', checklists[i])

    # 3. Judge each output separately against the same checklist.
    def judge(pair, target, ids):
        payload = {'source': pair['source'], 'candidate': pair[target], 'checklist': [views[sid] for sid in ids]}
        return api.call(stage_prompts['judge'], payload, 'judgments', contracts.judge_schema(ids),
                        lambda r: contracts.validate_judge(r, pair['source'], pair[target], ids))

    jobs = {}
    for i, record in checklists.items():
        for target in TARGETS:
            for g, ids in enumerate(batches(record['checklist'], values['judge_batch_size'])):
                jobs[(i, 'judge', target, g)] = lambda p=by_index[i], t=target, ids=ids: judge(p, t, ids)
    results = phase(jobs)
    records = []
    for i, record in checklists.items():
        if i in failures:
            continue
        groups_count = len(batches(record['checklist'], values['judge_batch_size']))
        judgments = {t: [row for g in range(groups_count) for row in results[(i, 'judge', t, g)]['judgments']] for t in TARGETS}
        scored = {'index': i, 'pair_id': by_index[i]['id'], **score_pair(record['checklist'], judgments)}
        write_json(folder / 'judgments' / f'{i}.json', {'pair_id': by_index[i]['id'], **judgments})
        write_json(folder / 'pairs' / f'{i}.json', scored)
        records.append(scored)
    failed = [{'index': i, 'pair_id': by_index[i]['id'], 'errors': errors} for i, errors in sorted(failures.items())]
    write_json(folder / 'failures.json', failed)
    summary = {'complete': not failed, 'failed_pairs': len(failed), **(aggregate(records) if records else {'pairs': 0})}
    write_json(folder / 'summary.json', summary)
    print(json.dumps({'output': str(folder), **summary}, indent=2))
    return summary


def load_run(folder):
    """Saved checklists, judgments and pair scores keyed by pair index."""
    folder = Path(folder)
    indices = sorted(int(path.stem) for path in (folder / 'pairs').glob('*.json'))
    return {i: {name: read_json(folder / name / f'{i}.json') for name in ('checklists', 'judgments', 'pairs')} for i in indices}
