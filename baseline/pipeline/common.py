"""Shared helpers for the end-to-end pipeline: config, prompts, LLM calls, ledger, files.

Rules carried over from CLAUDE.md:
- outputs are never overwritten: every stage writes under runs/<dataset>/<tag>/<stage>/ and
  resumes from existing rows; a finished stage is skipped
- API stages print calls / cost first and only call the API when --yes is given
- the ledger gets one line per API batch, counting only calls made in that batch
- prompt text lives in prompts/*.yaml (templates) + <track>/prompts/*.yaml (instances)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parents[1]
# Pairs per (domain, task) cell. 160 for the full study; set EVAL_N_PAIRS for the bundled sample data.
N_PAIRS = int(os.environ.get('EVAL_N_PAIRS', '160'))
LEDGER = ROOT / "logs" / "api_usage_ledger.jsonl"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENAI_PREFIX = "openai/"
PRICES = {  # USD per token (input, output); sonnet back-solved from the ledger of 2026-10-02
    "openai/gpt-4o-mini-2024-07-18": (0.00000015, 0.0000006),
    "hosted_vllm/Qwen/Qwen3.8-27B": (0.0, 0.0),  # Local GPU inference; excludes hardware/electricity cost
    # OpenAI list prices, checked on developers.openai.com/api/docs/models/<model> on 2026-10-07
    "openai/gpt-6-luna": (0.0000001, 0.0000005),
    "openai/gpt-6-sol": (0.000002, 0.00001),
    "openai/gpt-6.1-sol": (0.000002, 0.00001),
    "openai/gpt-5.6-luna": (0.0000002, 0.0000012),
    "openai/gpt-5.6-terra": (0.000002, 0.000012),
    "anthropic/claude-haiku-5.5": (0.0000001, 0.0000005),  # OpenRouter catalog, 2026-10-10
    "anthropic/claude-sonnet-5.5": (0.000002, 0.00001),
    "google/gemini-3.1-flash-lite": (0.00000025, 0.0000015),
    "google/gemini-3.1-pro-preview": (0.000002, 0.000012),  # OpenRouter public model catalog, 2026-10-08; inputs <200K
    "deepseek/deepseek-v4.1-flash": (0.00000013, 0.00000052),  # OpenRouter model page, 2026-10-11; provider-dependent estimate
    "deepseek/deepseek-v4-pro": (0.0000002088, 0.0000004176),  # OpenRouter catalog, 2026-10-08; provider-dependent estimate
    "google/gemini-3.8-flash": (0.00000075, 0.00000375),  # OpenRouter /models listing, 2026-10-03
    "qwen/qwen3.8-27b": (0.000000024, 0.00000435),  # OpenRouter model page, 2026-10-04
}


# ---------------------------------------------------------------- provider routing
def is_openai(model: str) -> bool:
    """GPT-family models always go through the OpenAI API with OPENAI_API_KEY, never through OpenRouter."""
    return model.startswith(OPENAI_PREFIX)


def client_args(model: str) -> dict:
    if model.startswith('hosted_vllm/'):
        return {'api_key': os.getenv('HOSTED_VLLM_API_KEY') or 'EMPTY',
                'base_url': os.getenv('HOSTED_VLLM_API_BASE') or 'http://localhost:8000/v1'}
    if is_openai(model):
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set in .env; GPT models are never routed through OpenRouter")
        return {"api_key": key}
    return {"api_key": os.getenv("OPENROUTER_API_KEY"), "base_url": OPENROUTER_BASE_URL}


def api_model(model: str) -> str:
    if model.startswith('hosted_vllm/'):
        return model[len('hosted_vllm/'):]
    return model[len(OPENAI_PREFIX):] if is_openai(model) else model


def request_kwargs(model: str, params: dict) -> dict:
    """Per-request kwargs shared by the batch runner and Seq.

    OpenAI direct (docs: developers.openai.com/api/docs/guides/latest-model, 2026-10-07): `reasoning_effort` and
    `max_completion_tokens`; sampling parameters such as temperature are only accepted when the effort is "none".
    OpenRouter keeps the reasoning settings in extra_body.
    """
    kw: dict = {}
    if model == 'openai/gpt-4o-mini-2024-07-18':
        # Non-reasoning paper snapshot: never send reasoning_effort.
        kw['max_tokens'] = params.get('max_tokens', 3000)
        if params.get('temperature') is not None:
            kw['temperature'] = params['temperature']
        if params.get('json', True):
            kw['response_format'] = {'type': 'json_object'}
        return kw
    if model.startswith('hosted_vllm/'):
        kw['max_tokens'] = params.get('max_tokens', 6000)
        kw['temperature'] = params.get('temperature', 0)
        if params.get('reasoning_off'):
            kw['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False}}
        if params.get('json', True):
            kw['response_format'] = {'type': 'json_object'}
        return kw
    if is_openai(model):
        effort = "none" if params.get("reasoning_off") else params.get("reasoning_effort")
        if effort:
            kw["reasoning_effort"] = effort
        if params.get("temperature") is not None and effort == "none":
            kw["temperature"] = params["temperature"]
        kw["max_completion_tokens"] = params.get("max_tokens", 6000)
    else:
        if params.get("temperature") is not None:
            kw["temperature"] = params["temperature"]
        if params.get("reasoning_off"):
            kw["extra_body"] = {"reasoning": {"enabled": False}}
        elif params.get("reasoning_effort"):  # for models where reasoning cannot be disabled (e.g. claude-sonnet-5.5)
            kw["extra_body"] = {"reasoning": {"effort": params["reasoning_effort"]}}
        kw["max_tokens"] = params.get("max_tokens", 6000)
    if params.get("json", True):
        kw["response_format"] = {"type": "json_object"}
    return kw


# ---------------------------------------------------------------- config / files
class Ctx:
    """Run context: config, paths, flags."""

    def __init__(self, config_path: str, tag: str, yes: bool, human_eval: bool):
        self.config_path = ROOT / config_path
        self.cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.tag = tag
        self.yes = yes
        self.human_eval = human_eval
        self.run_dir = ROOT / "runs" / self.cfg["dataset"] / tag
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def stage_dir(self, name: str) -> Path:
        d = self.run_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def model(self, role: str) -> str:
        return self.cfg["models"][role]

    def params(self, role: str) -> dict:
        return dict(self.cfg.get("model_params", {}).get(role, {}))

    def prompt(self, name: str) -> tuple[dict, dict]:
        """Return (template, instance) for prompt `name` (instance file = <instances>/<name>_<dataset>.yaml)."""
        inst_path = ROOT / self.cfg["instances_dir"] / f"{name}_{self.cfg['dataset']}.yaml"
        inst = yaml.safe_load(inst_path.read_text(encoding="utf-8")) if inst_path.exists() else {}
        tpl_path = ROOT / (inst.get("template") or f"prompts/{name}.yaml")
        # config slot_overrides replace instance slot values that exist (e.g. task_name without the question)
        for k, v in (self.cfg.get("slot_overrides") or {}).items():
            if k in inst.get("slots", {}):
                inst["slots"][k] = v
        return yaml.safe_load(tpl_path.read_text(encoding="utf-8")), inst

    def slots(self) -> dict:
        return dict(self.cfg.get("slots", {}))


def read_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json_new(p: Path, obj: Any) -> None:
    if p.exists():
        raise SystemExit(f"already exists: {p} (no overwrite; use a new --tag)")
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def atomic_write(path: Path, text: str, encoding: str = 'utf-8') -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding=encoding, newline='') as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def git_branch() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def numbered(d: dict[str, str]) -> str:
    return "\n".join(f"[{k}] {str(d[k]).strip()}" for k in sorted(d, key=lambda x: int(x) if str(x).isdigit() else x))


def parse_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    return json.loads(t[t.find("{"): t.rfind("}") + 1])


# ---------------------------------------------------------------- cost / ledger
def estimate(model: str, jobs: list[dict], out_tokens_per_call: int) -> tuple[int, float]:
    cin, cout = PRICES[model]
    ein = sum(len(j["system"]) + len(j["user"]) for j in jobs) / 4
    return len(jobs), ein * cin + len(jobs) * out_tokens_per_call * cout


def ledger(ctx: Ctx, model: str, usage: dict, params: dict, purpose: str) -> None:
    if not usage["calls"]:
        return
    cin, cout = PRICES[model]
    e = {"type": "api_call_batch", "timestamp": datetime.now(timezone.utc).astimezone().isoformat(), "commit": git_commit(),
         "branch": git_branch(), "script": ctx.cfg.get("ledger_script", "unknown"), "model": model,
         "provider": "hosted_vllm" if model.startswith('hosted_vllm/') else "openai" if is_openai(model) else "openrouter", "calls": usage["calls"],
         "input_tokens": usage["in"], "output_tokens": usage["out"], "total_tokens": usage["in"] + usage["out"],
         "cost_usd": round(usage["in"] * cin + usage["out"] * cout, 6), "params": params,
         "purpose": f"pipeline {ctx.cfg['dataset']}/{ctx.tag}: {purpose}"}
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"  ledger: {usage['calls']} calls ${e['cost_usd']}")


# ---------------------------------------------------------------- LLM batch
def gate(ctx: Ctx, label: str, model: str, jobs: list[dict], out_tokens: int, path: Path) -> list[dict]:
    """Print cost for pending jobs; return pending jobs if --yes, else []."""
    done = {r["key"] for r in read_jsonl(path)}
    pending = [j for j in jobs if j["key"] not in done]
    n, cost = estimate(model, pending, out_tokens)
    print(f"  [{label}] {model}: {len(jobs)} jobs, pending {n}, est. ${cost:.3f} -> {path.relative_to(ROOT)}")
    if pending and not ctx.yes:
        print("  (dry-run: add --yes to call the API)")
        return []
    return pending


def run_batch(ctx: Ctx, label: str, model: str, params: dict, pending: list[dict],
              handle: Callable[[dict, str], dict], path: Path, concurrency: int = 12, retries: int = 4,
              sdk_retries: int = 2) -> None:
    if not pending:
        return
    usage = asyncio.run(_run(model, params, pending, handle, path, concurrency, retries, sdk_retries))
    ledger(ctx, model, usage, params, label)


async def _run(model, params, pending, handle, path, concurrency, retries, sdk_retries):
    from dotenv import load_dotenv
    from openai import AsyncOpenAI
    from tqdm import tqdm
    load_dotenv(ROOT / ".env")
    import httpx
    from pipeline.request_slots import async_slot, async_pace
    client = AsyncOpenAI(**client_args(model), max_retries=sdk_retries,
                         timeout=httpx.Timeout(float(params.get('request_timeout',180)),connect=30))
    sem, lock = asyncio.Semaphore(min(concurrency,25)), asyncio.Lock()
    usage = {"calls": 0, "in": 0, "out": 0}
    fails: list[str] = []
    bar = tqdm(total=len(pending), leave=False)
    kw = request_kwargs(model, params)

    async def worker(j):
        last = None
        validation_feedback = None
        request_kw = dict(kw)
        for a in range(retries + 1):
            phase = 'request'
            started = None
            try:
                async with sem, async_slot():
                    await async_pace(model)
                    started = asyncio.get_running_loop().time()
                    user = j['user']
                    if validation_feedback and params.get('validation_retry_instruction'):
                        user += '\n\nOutput correction: ' + validation_feedback + '\n' + params['validation_retry_instruction']
                    request = client.chat.completions.create(
                        model=api_model(model), **request_kw,
                        messages=[{"role": "system", "content": j["system"]}, {"role": "user", "content": user}])
                    if params.get('request_timeout'):
                        r = await asyncio.wait_for(request, timeout=float(params['request_timeout']))
                    else:
                        r = await request
                phase = 'validation'
                response_usage = {"input_tokens": getattr(r.usage, "prompt_tokens", 0) or 0,
                                  "output_tokens": getattr(r.usage, "completion_tokens", 0) or 0}
                # A returned response is billable even when checklist validation fails.
                async with lock:
                    usage["calls"] += 1
                    usage["in"] += response_usage["input_tokens"]
                    usage["out"] += response_usage["output_tokens"]
                raw_response = r.choices[0].message.content or ""
                try:
                    row = handle(j, raw_response)
                except Exception as validation_error:
                    validation_feedback = str(validation_error)
                    if r.choices[0].finish_reason == 'length' and params.get('retry_max_tokens'):
                        token_key = 'max_completion_tokens' if 'max_completion_tokens' in request_kw else 'max_tokens'
                        current_limit = request_kw.get(token_key, 5000)
                        request_kw[token_key] = min(current_limit * 2, int(params['retry_max_tokens']))
                        print(f'  Truncated response {j["key"]}: retry output limit {request_kw[token_key]}', flush=True)
                    diagnostic = dict(key=j['key'], attempt=a + 1, model=model,
                                      error=str(validation_error), raw_response=raw_response,
                                      finish_reason=r.choices[0].finish_reason, usage=response_usage)
                    async with lock:
                        with path.with_name(path.stem + '_validation_errors.jsonl').open('a', encoding='utf-8') as f:
                            f.write(json.dumps(diagnostic, ensure_ascii=False) + '\n')
                    raise
                row.update({"key": j["key"], "usage": response_usage})
                async with lock:
                    with path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                bar.update(1)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                elapsed = asyncio.get_running_loop().time() - started if started is not None else 0
                error = str(e) or type(e).__name__
                diagnostic = dict(key=j['key'], attempt=a + 1, model=model, phase=phase,
                                  elapsed_seconds=round(elapsed, 2), error=error)
                async with lock:
                    with path.with_name(path.stem + '_request_errors.jsonl').open('a', encoding='utf-8') as f:
                        f.write(json.dumps(diagnostic, ensure_ascii=False) + '\n')
                print(f'  {phase} failed {j["key"]} ({elapsed:.1f}s), attempt {a + 1}/{retries + 1}: {error}', flush=True)
                await asyncio.sleep(min(2 ** a, 8))
        fails.append(f"{j['key']}: {last}")

    try:
        await asyncio.gather(*(worker(j) for j in pending))
    finally:
        bar.close()
        await client.close()
    if fails:
        print("  FAILED (re-run the stage to retry):\n    " + "\n    ".join(fails[:20]))
    return usage


class Seq:
    """Sequential calls whose next prompt depends on earlier answers (e.g. RRD filters against the accepted set).
    One ledger line per `with` block, written on exit even if a call fails."""

    def __init__(self, ctx: Ctx, label: str, model: str, params: dict, retries: int = 4,
                 retry_transient_only: bool = False):
        self.ctx, self.label, self.model, self.params, self.retries = ctx, label, model, params, retries
        self.retry_transient_only = retry_transient_only
        self.usage = {"calls": 0, "in": 0, "out": 0}

    def __enter__(self):
        from dotenv import load_dotenv
        from openai import OpenAI
        load_dotenv(ROOT / ".env")
        import httpx
        self.client = OpenAI(**client_args(self.model),max_retries=0,
                            timeout=httpx.Timeout(float(self.params.get('request_timeout',180)),connect=30))
        return self

    def call(self, system: str, user: str, handle: Callable[[str], dict]) -> dict:
        import time
        from pipeline.request_slots import sync_slot, sync_pace
        kw, last = request_kwargs(self.model, self.params), None
        for a in range(self.retries + 1):
            try:
                with sync_slot():
                    sync_pace(self.model)
                    r = self.client.chat.completions.create(model=api_model(self.model), **kw,
                                                            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
                self.usage["calls"] += 1
                self.usage["in"] += r.usage.prompt_tokens or 0
                self.usage["out"] += r.usage.completion_tokens or 0
                return handle(r.choices[0].message.content or "")
            except Exception as e:  # noqa: BLE001
                last = e
                if a >= self.retries:
                    break
                delay = min(2 ** a, 8)
                if self.retry_transient_only:
                    from openai import APIConnectionError, APITimeoutError
                    status = getattr(e, 'status_code', None)
                    if not (status in (408, 409, 429) or isinstance(status, int) and status >= 500
                            or isinstance(e, (APIConnectionError, APITimeoutError))):
                        break
                    response = getattr(e, 'response', None)
                    headers = getattr(response, 'headers', {})
                    body = getattr(e, 'body', None)
                    retry_after = headers.get('retry-after') or (body.get('retry_after') if isinstance(body, dict) else None)
                    try:
                        delay = min(max(delay, float(retry_after)), 120)
                    except (TypeError, ValueError):
                        delay = max(delay, 10)
                    print(f'{self.label}: transient HTTP {status or "connection"}; retry {a+1}/{self.retries} in {delay:g}s', flush=True)
                time.sleep(delay)
        raise RuntimeError(f"{self.label}: failed after retries: {last}")

    def __exit__(self, *exc):
        self.client.close()
        ledger(self.ctx, self.model, self.usage, self.params, self.label)
        return False
