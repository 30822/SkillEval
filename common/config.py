"""Validated project configuration and replaceable domain prompt modules."""
from importlib import import_module
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
POSITIVE = ('workers', 'max_output_tokens', 'evaluation_pairs', 'match_batch_size', 'judge_batch_size')


def load_config(path=None):
    source = Path(path) if path else ROOT / 'config.yaml'
    with source.open(encoding='utf-8') as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError(f'{source}: expected a mapping')
    for name, spec in config['datasets'].items():
        if spec['domain'] not in ('finance', 'medical', 'law') or spec['task'] not in ('dtt', 'rewriting', 'summary'):
            raise ValueError(f'Invalid domain/task: {name}')
        if not isinstance(spec['prompt_module'], str):
            raise ValueError(f'Prompt module required: {name}')
        for key, value in spec.get('settings', {}).items():
            if key not in config['defaults']:
                raise ValueError(f'{name}.settings.{key} has no default')
    for key in POSITIVE:
        if type(config['defaults'][key]) is not int or config['defaults'][key] < 1:
            raise ValueError(f'Positive integer required: defaults.{key}')
    for key in ('max_output_tokens', 'limit'):
        if type(config['generation'][key]) is not int or config['generation'][key] < 1:
            raise ValueError(f'Positive integer required: generation.{key}')
    return config


def prompt_module(spec):
    module = import_module(spec['prompt_module'])
    if not isinstance(module.GENERATION, str) or not module.GENERATION.strip():
        raise ValueError('Prompt module must define nonempty GENERATION')
    return module
