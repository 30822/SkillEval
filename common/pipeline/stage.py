"""Settings, prompts and run-folder bookkeeping shared by the stage runners."""
import hashlib
import json
from pathlib import Path
from common.config import ROOT, prompt_module
from common.pipeline.contracts import DEDUPE, system_prompt
from common.pipeline.llm import Client, load_env, model_settings, read_json, write_json


def settings(config, dataset, overrides):
    spec = config['datasets'][dataset]
    values = {**config['defaults'], **spec.get('settings', {})}
    values.update({key: value for key, value in overrides.items() if value is not None})
    return values


def prompts(spec):
    """Stage prompts of a task module; EXTRACT may be one prompt or {pass_name: prompt}."""
    module = prompt_module(spec)
    missing = [name for name in ('EXTRACT', 'MATCH', 'JUDGE') if not getattr(module, name, None)]
    if missing:
        raise ValueError(f"{spec['prompt_module']} must define {', '.join(missing)}")
    extract = module.EXTRACT if isinstance(module.EXTRACT, dict) else {'main': module.EXTRACT}
    return {'extract': {name: system_prompt(text, 'extract') for name, text in extract.items()},
            'match': system_prompt(module.MATCH, 'match'),
            'dedupe': system_prompt(getattr(module, 'DEDUPE', None) or DEDUPE, 'dedupe'),
            'judge': system_prompt(module.JUDGE, 'judge')}


def sha256(value):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()


def output_dir(spec, stage, output):
    return Path(output) if output else ROOT / 'runs' / spec['domain'] / spec['task'] / 'pipeline' / stage


def open_run(folder, manifest):
    """Refuse to mix runs whose model, prompts, inputs or settings differ."""
    path = folder / 'manifest.json'
    if path.exists() and read_json(path) != manifest:
        raise ValueError(f'{path} records a different configuration; choose a new --output')
    write_json(path, manifest)


def client(config, values, folder):
    load_env()
    return Client(model_settings(config, values['model']), folder / 'cache', max_tokens=values['max_output_tokens'])


def batches(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]
