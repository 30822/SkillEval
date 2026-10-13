"""Run inputs, pairing and aggregation helpers shared by every evaluator stage.

A run lives in runs/<dataset>/<version>/ and is frozen by two files:
  split/split.json     {"eval": [{id, input, source: {unit: text}, gt: {unit: text}, meta}]}
  generate/llm.jsonl   {id, llm: {unit: text}} per case
Every evaluator scores both candidates (gt = human, llm = model) of each case; `summarize` turns per-side
scores into win/tie counts and an exact sign test.
"""
import csv
import hashlib
import json
import math
from collections import Counter

from pipeline import common as C

SIDES = ('gt', 'llm')


def write_same(path, text):
    """Write once; an existing file must be byte-identical (frozen inputs are never overwritten)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with path.open(encoding='utf-8', newline='') as existing:
            unchanged = existing.read() == text
        if not unchanged:
            raise ValueError(f'Run inputs changed: {path}; use a new tag')
    else:
        path.write_text(text, encoding='utf-8')


def write_snapshot(path, text):
    """Config snapshot of a run; a changed config under the same tag is kept next to the earlier file."""
    if path.exists() and path.read_text(encoding='utf-8') != text:
        path = path.with_name('%s_%s%s' % (path.stem, hashlib.sha256(text.encode('utf-8')).hexdigest()[:8], path.suffix))
    write_same(path, text)


def sign_p(a, b):
    """Two-sided exact sign test on non-tied pairs."""
    n = a + b
    if not n:
        return None
    k = min(a, b)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def load_cases(cfg, limit=None):
    """Evaluation cases with human (gt) and model (llm) outputs as unit dicts {unit id: text}."""
    src = C.ROOT / cfg['v3_run']
    cases = C.read_json(src / 'split/split.json')['eval']
    if limit:
        cases = cases[:limit]
    llm = {r['id']: r['llm'] for r in C.read_jsonl(src / 'generate/llm.jsonl')}
    missing = [c['id'] for c in cases if c['id'] not in llm]
    if missing:
        raise ValueError(f'{len(missing)} evaluation cases have no LLM output yet')
    out = []
    for c in cases:
        source, outputs = c['source'], {'gt': c['gt'], 'llm': llm[c['id']]}
        assert set(source) == set(outputs['gt']) == set(outputs['llm']), c['id']
        ids = sorted(source, key=int)
        both = [i for i in ids if all(str(outputs[s][i]).strip() for s in SIDES)]
        out.append({'id': c['id'], 'ids': ids, 'paired_ids': both, 'source': source, 'input': c.get('input', ''),
                    'meta': c.get('meta', {}), **outputs})
    return out


def join(d, ids):
    return ' '.join(str(d[i]).strip() for i in ids if str(d[i]).strip())


def summarize(metric, mcfg, rows, cases, d):
    """rows: [{id, side, <primary>}] -> summary.json and per_case.csv in directory d."""
    by = {(r['id'], r['side']): r for r in rows}
    key = mcfg['primary']
    wins, per_case = Counter(), []
    for c in cases:
        g, l = by[(c['id'], 'gt')][key], by[(c['id'], 'llm')][key]
        w = 'gt' if g > l else 'llm' if l > g else 'tie'
        wins[w] += 1
        per_case.append({'id': c['id'], 'n_units': len(c['ids']), 'n_paired': len(c['paired_ids']),
                         'gt': g, 'llm': l, 'diff_gt_minus_llm': g - l, 'winner': w})
    n = len(cases)
    summary = {'metric': metric, 'primary_score': key, 'unit': mcfg.get('unit', 'document'), 'n_pairs': n,
               'winner_rule': 'higher primary score wins; exact equality is a tie',
               'winners': {k: wins[k] for k in ('gt', 'llm', 'tie')}, 'sign_p': sign_p(wins['gt'], wins['llm']),
               'mean_gt': sum(r['gt'] for r in per_case) / n, 'mean_llm': sum(r['llm'] for r in per_case) / n}
    d.mkdir(parents=True, exist_ok=True)
    write_same(d / 'summary.json', json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    p = d / 'per_case.csv'
    if not p.exists():
        with p.open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(per_case[0]))
            w.writeheader()
            w.writerows(per_case)
    return summary
