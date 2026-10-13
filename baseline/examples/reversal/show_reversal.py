"""Print the bundled judgment-reversal examples: human text vs LLM text and every judge's preference.

    python examples/reversal/show_reversal.py
"""
import json
import textwrap
from pathlib import Path


def main():
    doc = json.loads((Path(__file__).with_name('reversal_examples.json')).read_text(encoding='utf-8'))
    print(textwrap.fill(doc['description'], 100), '\n')
    for p in doc['pairs']:
        print('=' * 100)
        print(f"{p['id']}  [{p['cell']}]  {p['task']}")
        print('-' * 100)
        print('INPUT (first 400 chars):', p['input'][:400].replace('\n', ' '), '...')
        print('\nHUMAN:\n' + textwrap.fill(p['human'], 100))
        print('\nLLM:\n' + textwrap.fill(p['llm'], 100))
        ev = p['evidence']
        print('\nPairwise (gpt6-luna, both presentation orders):')
        for order, v in sorted(ev['pairwise_gpt6-luna_both_orders'].items()):
            print(f"  {order:10s} picked={v['picked']:4s} {v['reason'][:90]}")
        print('\nG-Eval general axes per judge (human vs LLM, 0-1):')
        wins = 0
        for judge, v in ev['geval_general_axes_per_judge'].items():
            wins += v['llm_geval'] > v['human_geval']
            print(f"  {judge:22s} human={v['human_geval']:.3f}  llm={v['llm_geval']:.3f}")
        print(f"  -> {wins}/{len(ev['geval_general_axes_per_judge'])} judges prefer the LLM text\n")


if __name__ == '__main__':
    main()
