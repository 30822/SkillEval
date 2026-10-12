"""Strict-JSON chat client with a request-keyed disk cache and validation retries."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from common.config import ROOT

ENDPOINTS = {'openai': ('https://api.openai.com/v1/chat/completions', 'OPENAI_API_KEY'),
             'openrouter': ('https://openrouter.ai/api/v1/chat/completions', 'OPENROUTER_API_KEY')}
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}


def load_env(path=ROOT / '.env'):
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key and not key.startswith('#'):
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def model_settings(config, name=None):
    """Resolve a `models` key (or a raw model ID) into id/provider/reasoning."""
    name = name or config['defaults']['model']
    for key, spec in config['models'].items():
        if name in (key, spec['id']):
            return {'id': spec['id'], 'provider': spec['provider'], 'reasoning': spec.get('reasoning')}
    return {'id': name, 'provider': 'openai', 'reasoning': None}


class Client:
    """One strict json_schema chat call per request; identical requests read the cache."""

    def __init__(self, model, cache_dir, max_tokens=16000, attempts=3, timeout=600):
        self.model, self.cache_dir = model, Path(cache_dir)
        self.max_tokens, self.attempts, self.timeout = max_tokens, attempts, timeout

    def body(self, system, payload, schema_name, schema):
        body = {'model': self.model['id'],
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                'response_format': {'type': 'json_schema', 'json_schema': {'name': schema_name, 'strict': True, 'schema': schema}}}
        tokens = 'max_completion_tokens' if self.model['provider'] == 'openai' else 'max_tokens'
        body[tokens] = self.max_tokens
        effort = self.model.get('reasoning')
        if effort in ('none', 'minimal', 'low', 'medium', 'high'):
            if self.model['provider'] == 'openai':
                body['reasoning_effort'] = effort
            else:
                body['reasoning'] = {'effort': effort} if effort != 'none' else {'enabled': False}
        return body

    def call(self, system, payload, schema_name, schema, validate):
        """Return a validated result; invalid answers are retried with the error as feedback."""
        body = self.body(system, payload, schema_name, schema)
        key = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.cache_dir / f'{key}.json'
        if path.exists():
            result = read_json(path)['result']
            validate(result)
            return result
        messages = list(body['messages'])
        error = None
        for _ in range(self.attempts):
            raw = self._post({**body, 'messages': messages})
            choice = raw['choices'][0]
            if choice.get('finish_reason') not in (None, 'stop'):
                raise RuntimeError(f"Incomplete response: {choice.get('finish_reason')}")
            content = choice['message']['content']
            try:
                result = json.loads(content)
                validate(result)
            except (ValueError, KeyError, TypeError) as caught:
                error = caught
                messages = [*body['messages'], {'role': 'assistant', 'content': content},
                            {'role': 'user', 'content': f'Validation error: {caught}. Return the complete corrected JSON.'}]
                continue
            write_json(path, {'result': result, 'model': raw.get('model'), 'usage': raw.get('usage'), 'request': body})
            return result
        raise ValueError(f'Invalid response after {self.attempts} attempts: {error}')

    def _post(self, body):
        url, variable = ENDPOINTS[self.model['provider']]
        token = os.environ.get(variable)
        if not token:
            raise RuntimeError(f'{variable} is not set')
        data = json.dumps(body).encode()
        for attempt in range(6):
            request = urllib.request.Request(url, data=data, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in RETRY_STATUS or attempt == 5:
                    detail = error.read().decode(errors='replace').replace(token, '[redacted]')[:1000]
                    raise RuntimeError(f'HTTP {error.code}: {detail}') from None
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == 5:
                    raise RuntimeError(f'Transport failed: {error}') from None
            time.sleep(min(60, 2 ** (attempt + 1)) + random.random())


def run_jobs(jobs, workers):
    """Run {key: callable}; return (results, errors) without stopping on one failure."""
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(job): key for key, job in jobs.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as error:
                errors[key] = f'{type(error).__name__}: {error}'
    return results, errors
