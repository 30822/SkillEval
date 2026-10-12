"""Offline re-check of an evaluation run: no API calls, every saved record re-validated and re-scored."""
import json
from pathlib import Path
from common.config import ROOT
from common.pipeline import contracts
from common.pipeline.data import load_pairs
from common.pipeline.evaluate import build_checklist, load_run
from common.pipeline.llm import read_json, write_json
from common.pipeline.scoring import TARGETS, aggregate, score_pair
from common.pipeline.stage import output_dir, sha256
from common.skill_schema import load_skills


def run(config, dataset, *, output=None, **_):
    spec = config['datasets'][dataset]
    folder = Path(output) if output else output_dir(spec, 'evaluation', None)
    manifest = read_json(folder / 'manifest.json')
    bank = load_skills(Path(manifest['bank']) if Path(manifest['bank']).is_absolute() else ROOT / manifest['bank'], spec['domain'], spec['task'])
    if sha256(bank) != manifest['bank_sha256']:
        raise ValueError('Skill bank changed since the evaluation run')
    skill_ids = [skill['id'] for skill in bank['skills']]
    pairs = {pair['index']: pair for pair in load_pairs(dataset, 'test', manifest['test_pairs'])}
    saved = load_run(folder)
    records = []
    for i, run_record in saved.items():
        pair, checklist, judgments = pairs[i], run_record['checklists'], run_record['judgments']
        contracts.validate_match({'matches': checklist['matches']}, pair['source'], skill_ids)
        matches = {row['skill_id']: row for row in checklist['matches']}
        applicable = build_checklist(skill_ids, matches, None)
        decisions = None
        if checklist['decisions']:
            contracts.validate_dedupe({'decisions': checklist['decisions']}, applicable)
            decisions = {row['skill_id']: row for row in checklist['decisions']}
        elif len(applicable) > 1:
            raise ValueError(f'Pair {i}: missing dedupe decisions')
        if build_checklist(skill_ids, matches, decisions) != checklist['checklist']:
            raise ValueError(f'Pair {i}: checklist differs from its matches and decisions')
        for target in TARGETS:
            contracts.validate_judge({'judgments': judgments[target]}, pair['source'], pair[target], checklist['checklist'])
        scored = {'index': i, 'pair_id': pair['id'], **score_pair(checklist['checklist'], judgments)}
        if scored != run_record['pairs']:
            raise ValueError(f'Pair {i}: saved score differs from recomputation')
        records.append(scored)
    summary = read_json(folder / 'summary.json')
    recomputed = {'complete': not read_json(folder / 'failures.json'), 'failed_pairs': len(read_json(folder / 'failures.json')),
                  **(aggregate(records) if records else {'pairs': 0})}
    if recomputed != summary:
        raise ValueError('summary.json differs from recomputation')
    result = {'api_calls': 0, 'verified_pairs': len(records), 'complete': summary['complete'],
              'missing_pairs': sorted(set(pairs) - set(saved)), **{k: summary[k] for k in ('human_wins', 'ties', 'llm_wins', 'jrr') if k in summary}}
    write_json(folder / 'verification.json', result)
    print(json.dumps({'output': str(folder), **result}, indent=2))
    return result
