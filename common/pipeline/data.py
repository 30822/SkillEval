"""TRAIN/TEST rows as uniform {id, source, human, llm} pairs.

Every data/<dataset>/<split>.json is a list of {id, source, human} rows; TEST rows
also carry `llm`. `source` is a string for text tasks and a JSON object for
data-to-text tasks. The evaluation `source` is exactly the generation input
(objects become indented JSON text), so quotes from the source are checked
against the same string the model sees.
"""
import json
from pathlib import Path
from common.config import ROOT


def read_rows(path):
    rows = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(rows, list):
        raise ValueError(f'{path}: expected a list of rows')
    return rows


def _text(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing or empty field: {key}')
    return value


def source_payload(row):
    """Generation input; never contains a human or model output."""
    source = row.get('source')
    if isinstance(source, dict) and source:
        return source
    return {'source_text': _text(row, 'source')}


def source_text(row):
    payload = source_payload(row)
    if set(payload) == {'source_text'}:
        return payload['source_text']
    return json.dumps(payload, ensure_ascii=False, indent=2)


def row_id(row, index):
    return str(row['id']) if row.get('id') not in (None, '') else str(index)


def load_pairs(dataset, split, limit=None, excluded=None):
    """Rows in file order; TEST rows also carry the generated `llm` text.

    TRAIN rows without a usable source or human text are skipped and reported in
    `excluded` when a list is given; any unusable TEST row is an error.
    """
    rows = read_rows(ROOT / 'data' / dataset / f'{split}.json')
    pairs = []
    for index, row in enumerate(rows):
        if limit and len(pairs) == limit:
            break
        try:
            pair = {'index': index, 'id': row_id(row, index), 'source': source_text(row), 'human': _text(row, 'human')}
        except ValueError as error:
            if split == 'test' or excluded is None:
                raise
            excluded.append({'index': index, 'id': row_id(row, index), 'reason': str(error)})
            continue
        if split == 'test':
            pair['llm'] = _text(row, 'llm')
        pairs.append(pair)
    if len({pair['id'] for pair in pairs}) != len(pairs):
        raise ValueError(f'{dataset}/{split}: duplicate row IDs')
    return pairs
