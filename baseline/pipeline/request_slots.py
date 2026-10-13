"""Workspace-wide request limit shared by independent evaluation terminals."""
from contextlib import asynccontextmanager, contextmanager
import asyncio
import fcntl
import hashlib
import random
import time

from pipeline.common import ROOT

LIMIT = 100
MODEL_RPM = {'google/gemini-3.8-flash': 240}


def pace_delay(model):
    """Reserve a start time across terminals for models with known RPM limits."""
    rpm = MODEL_RPM.get(model)
    if rpm is None:
        return 0
    directory = ROOT / 'reports/.api_request_slots'
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(model.encode()).hexdigest()
    with (directory / f'pace_{key}.lock').open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        previous = handle.read().strip()
        now = time.time()
        start = max(now, float(previous) if previous else now)
        handle.seek(0)
        handle.truncate()
        handle.write(str(start + 60 / rpm))
        handle.flush()
    return max(0, start - now)


async def async_pace(model):
    delay = pace_delay(model)
    if delay:
        await asyncio.sleep(delay)


def sync_pace(model):
    delay = pace_delay(model)
    if delay:
        time.sleep(delay)


def attempt(slot):
    directory = ROOT / 'reports/.api_request_slots'
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / f'{slot}.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        return None


@asynccontextmanager
async def async_slot():
    slot = random.randrange(LIMIT)
    handle = None
    try:
        while handle is None:
            handle = attempt(slot)
            slot = (slot + 1) % LIMIT
            if handle is None:
                await asyncio.sleep(0.1)
        yield
    finally:
        if handle is not None:
            handle.close()


@contextmanager
def sync_slot():
    slot = random.randrange(LIMIT)
    handle = None
    try:
        while handle is None:
            handle = attempt(slot)
            slot = (slot + 1) % LIMIT
            if handle is None:
                time.sleep(0.1)
        yield
    finally:
        if handle is not None:
            handle.close()
