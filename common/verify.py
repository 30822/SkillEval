"""Offline project check: skill catalogs, prompt modules and data for every domain/task (no API calls)."""
import json
from common.config import ROOT, load_config
from common.pipeline.data import load_pairs
from common.pipeline.stage import prompts
from common.skill_schema import CATALOG_VERSION, load_skills, validate_catalog


def verify():
    config = load_config()
    catalogs, datasets = {}, {}
    for name, spec in config['datasets'].items():
        path = ROOT / spec['skills']
        document = json.loads(path.read_text(encoding='utf-8'))
        if document.get('schema_version') == CATALOG_VERSION and path not in catalogs:
            catalogs[path] = validate_catalog(document)['skill_count']
        bank = load_skills(path, spec['domain'], spec['task'])
        if len(bank['skills']) != spec['skill_count']:
            raise ValueError(f'{name}: skill_count does not match {spec["skills"]}')
        stage_prompts = prompts(spec)
        excluded = []
        train = load_pairs(name, 'train', excluded=excluded)
        test = load_pairs(name, 'test', config['defaults']['evaluation_pairs'])
        if not train or not test:
            raise ValueError(f'{name}: TRAIN and TEST need at least one usable row')
        datasets[name] = {'skills': len(bank['skills']), 'extract_passes': list(stage_prompts['extract']),
                          'train_pairs': len(train), 'excluded_train_rows': len(excluded), 'test_pairs': len(test),
                          'sample_data': len(test) < config['defaults']['evaluation_pairs']}
    return {'api_calls': 0, 'catalogs': {str(path.relative_to(ROOT)): count for path, count in catalogs.items()}, 'datasets': datasets}


if __name__ == '__main__':
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
