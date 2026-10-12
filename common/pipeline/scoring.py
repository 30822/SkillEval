"""Pass-count comparison of the human and LLM outputs on one shared checklist."""
from collections import Counter
from statistics import mean

TARGETS = ('human', 'llm')


def score_pair(checklist, judgments):
    """Every checklist item counts once for each target; more passes wins."""
    scores = {}
    for target in TARGETS:
        verdicts = {row['skill_id']: row['verdict'] for row in judgments[target]}
        if set(verdicts) != set(checklist):
            raise ValueError(f'{target}: judgments do not cover the checklist exactly')
        passed = sum(verdicts[skill] == 'pass' for skill in checklist)
        scores[target] = {'pass': passed, 'fail': len(checklist) - passed,
                          'pass_rate': passed / len(checklist) if checklist else None}
    human, llm = scores['human']['pass'], scores['llm']['pass']
    outcome = 'human_wins' if human > llm else 'llm_wins' if llm > human else 'tie'
    return {'checklist_items': len(checklist), **scores, 'outcome': outcome}


def aggregate(records):
    """Win/tie/loss over completed pairs; JRR is the LLM win rate."""
    if not records:
        raise ValueError('No completed pairs to aggregate')
    outcomes = Counter(record['outcome'] for record in records)
    sizes = [record['checklist_items'] for record in records]
    totals = {t: {k: sum(record[t][k] for record in records) for k in ('pass', 'fail')} for t in TARGETS}
    items = sum(sizes)
    return {'pairs': len(records), 'human_wins': outcomes['human_wins'], 'ties': outcomes['tie'],
            'llm_wins': outcomes['llm_wins'], 'jrr': outcomes['llm_wins'] / len(records),
            'checklist_items': items, 'mean_checklist_items': mean(sizes), 'min_checklist_items': min(sizes),
            'max_checklist_items': max(sizes), 'empty_checklist_pairs': sizes.count(0), 'totals': totals,
            'pass_rates': {t: totals[t]['pass'] / items if items else None for t in TARGETS}}
