"""Explicit operator replay of one saved event, without notifying or executing."""
import asyncio
import json
import ssl
import sys
import httpx
from bootstrap import ROOT, settings
from trader.store import Store
from trader.agent import Agent


async def run(event_id):
    store = Store(ROOT / 'data/recognition.sqlite3')
    try:
        row = store.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        if not row:
            raise ValueError('Missing event')
        async with httpx.AsyncClient(verify=ssl.create_default_context(), timeout=90) as client:
            result, trace = await Agent(settings(), client, store).analyze(json.loads(row['payload']), row['channel'], row['message'])
            (ROOT / 'data/replay-result.json').write_text(result.model_dump_json(indent=2), encoding='utf-8')
            print('Replay succeeded:', trace)
    finally:
        store.db.close()


if __name__ == '__main__':
    try:
        asyncio.run(run(int(sys.argv[1])))
    except Exception as error:
        print('Replay error:', type(error).__name__)
        sys.exit(1)
