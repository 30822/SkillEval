"""Offline walk through the pipeline skeleton: frozen run -> pairing -> cost gate -> aggregation.

    python examples/skeleton_demo.py            # nothing is sent
    python examples/skeleton_demo.py --yes      # would call the judge model (needs keys in .env)

Evaluators plug in at step 3: build one job per (case, side) or per case, let `gate` print the pending calls and
cost (dry run by default), `run_batch` the jobs, then `summarize` the per-side scores.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import common as C  # noqa: E402
from pipeline.results import SIDES, load_cases, summarize, write_same  # noqa: E402

PAIRS = [
    ('The permit was withdrawn in 2016.',
     'In 2016 the agency withdrew the permit after the facility closed.'),
    ('Penalty: $16,000 per violation.',
     'The agency proposed a penalty of up to $16,000 per violation for unpermitted discharge.'),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    ctx = C.Ctx('examples/skeleton_config.yaml', 'skeleton_demo_01', args.yes, False)

    # 1) Frozen inputs: the split (human reference = gt) and the model outputs.
    run = ctx.run_dir
    cases = [{'id': f'demo_{i}', 'input': 'Summarize the enforcement record.', 'source': {'1': f'record {i}'},
              'gt': {'1': human}, 'meta': {}} for i, (human, _) in enumerate(PAIRS)]
    write_same(run / 'split/split.json', json.dumps({'eval': cases}, indent=2) + '\n')
    write_same(run / 'generate/llm.jsonl',
               ''.join(json.dumps({'id': f'demo_{i}', 'llm': {'1': llm}}) + '\n' for i, (_, llm) in enumerate(PAIRS)))

    # 2) Pairing: both candidates of every case as unit dicts.
    loaded = load_cases({'v3_run': str(run.relative_to(C.ROOT))})
    print(f'{len(loaded)} cases, candidates per case: {SIDES}')

    # 3) Cost gate. Dry run prints the pending calls and estimated cost and returns no jobs.
    jobs = [dict(key=f"{c['id']}|{side}", system='Rate the text from 1 to 5.', user=c[side]['1'])
            for c in loaded for side in SIDES]
    out = ctx.stage_dir('llm') / 'toy_judge'
    pending = C.gate(ctx, 'toy judge', ctx.model('judge'), jobs, 50, out / 'judgments.jsonl')
    if pending:
        raise SystemExit('Live path: run_batch(ctx, label, model, params, pending, handle, path) goes here.')

    # 4) Aggregation (toy scores stand in for judge outputs).
    rows = [{'id': c['id'], 'side': s, 'score': float(len(c[s]['1']))} for c in loaded for s in SIDES]
    summary = summarize('toy_length', {'primary': 'score', 'unit': 'document'}, rows, loaded,
                        ctx.stage_dir('neural') / 'toy_length')
    print('winners:', summary['winners'], '| sign_p:', summary['sign_p'])


if __name__ == '__main__':
    main()
