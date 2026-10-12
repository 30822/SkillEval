"""Common skill-bank schema (skill-bank/v1) shared by every task bank and extraction output."""
import json
from pathlib import Path

SCHEMA_VERSION = 'skill-bank/v1'
DOMAINS = ('finance', 'medical', 'law')
TASKS = ('dtt', 'rewriting', 'summary')
FIELDS = ('id', 'domain', 'task', 'name', 'action', 'applicability', 'preserve', 'evaluation', 'evidence', 'provenance', 'extensions')


def validate(bank):
    if not isinstance(bank, dict) or bank.get('schema_version') != SCHEMA_VERSION or bank.get('domain') not in DOMAINS or bank.get('task') not in TASKS:
        raise ValueError('Invalid common bank header')
    if not isinstance(bank.get('skills'), list) or not isinstance(bank.get('metadata'), dict):
        raise ValueError('Bank skills array and metadata object required')
    ids = set()
    for skill in bank['skills']:
        if set(skill) != set(FIELDS):
            raise ValueError('Every skill must have the same common fields')
        if not isinstance(skill['id'], str) or not skill['id'] or skill['id'] in ids:
            raise ValueError('Skill IDs must be nonempty and unique')
        ids.add(skill['id'])
        if (skill['domain'], skill['task']) != (bank['domain'], bank['task']):
            raise ValueError('Skill domain/task mismatch')
        if not isinstance(skill['action'], str) or not skill['action'].strip():
            raise ValueError('Observed action required')
        if not isinstance(skill['preserve'], list) or not isinstance(skill['applicability'], dict):
            raise ValueError('Common applicability and preserve types required')
        for key in ('all_of', 'exclude_if'):
            if not isinstance(skill['applicability'][key], list):
                raise ValueError('Condition arrays required')
        if not isinstance(skill['evidence']['records'], list) or not isinstance(skill['evidence']['ids'], list):
            raise ValueError('Evidence record/ID arrays required')
    return bank


def load_bank(path):
    return validate(json.loads(Path(path).read_text(encoding='utf-8')))


CATALOG_VERSION = 'domain-skills/v1'
TOPICS = {
    'dtt': ('Data-to-text reporting', 'Express facts, relationships, and scope from structured input in report text.'),
    'rewriting': ('Rewriting', 'Improve wording, structure, and presentation while preserving source meaning and necessary information.'),
    'summary': ('Summarization', 'Summarize source text while preserving its key information and conditions.'),
}


def build_catalog(domain, banks):
    """One domain file holding each task's skills under topics.<task>.skills."""
    topics = {}
    for task, bank in banks.items():
        validate(bank)
        if (bank['domain'], bank['task']) != (domain, task):
            raise ValueError(f'Bank is not {domain}/{task}')
        title, purpose = TOPICS[task]
        topics[task] = {'title': title, 'purpose': purpose, 'skill_count': len(bank['skills']), 'skills': bank['skills']}
    return validate_catalog({'schema_version': CATALOG_VERSION, 'domain': domain,
                             'skill_count': sum(t['skill_count'] for t in topics.values()), 'topics': topics})


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or catalog.get('schema_version') != CATALOG_VERSION or catalog.get('domain') not in DOMAINS:
        raise ValueError('Invalid domain catalog header')
    if not catalog['topics'] or set(catalog['topics']) - set(TASKS):
        raise ValueError('Catalog topics must be known tasks')
    for task, topic in catalog['topics'].items():
        validate(task_bank(catalog, task))
        if topic['skill_count'] != len(topic['skills']):
            raise ValueError(f'{task}: skill_count does not match skills')
    if catalog['skill_count'] != sum(t['skill_count'] for t in catalog['topics'].values()):
        raise ValueError('Catalog skill_count does not match its topics')
    return catalog


def task_bank(catalog, task):
    """The task's skills from a domain catalog, as a skill-bank/v1 document."""
    if task not in catalog['topics']:
        raise ValueError(f"Catalog has no {task} skills")
    return {'schema_version': SCHEMA_VERSION, 'domain': catalog['domain'], 'task': task,
            'skills': catalog['topics'][task]['skills'], 'metadata': {}}


def load_skills(path, domain, task):
    """Skill bank for one task, read from a domain catalog or from a single task bank."""
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    bank = task_bank(validate_catalog(document), task) if document.get('schema_version') == CATALOG_VERSION else validate(document)
    if (bank['domain'], bank['task']) != (domain, task):
        raise ValueError(f'{path} does not hold {domain}/{task} skills')
    return bank
