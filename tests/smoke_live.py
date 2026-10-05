"""Explicit live model smoke test on synthetic text and optional user-provided image."""
import asyncio
from datetime import datetime, timezone
import json
import ssl
import sys

import httpx

from bootstrap import ROOT, settings
from trader.agent import Agent
from trader.store import Store


async def run():
    cfg = settings()
    store = Store(ROOT / 'data/recognition.sqlite3')
    try:
        output = []
        async with httpx.AsyncClient(verify=ssl.create_default_context(), timeout=90) as client:
            agent = Agent(cfg, client, store)
            original = {'id': 1, 'text': 'BRUSDT шорт 20x. Пробую. Цена входа 0.89378.',
                        'date': datetime.now(timezone.utc).isoformat(), 'reply_to': None, 'media': []}
            image = ROOT / 'data/smoke/br-entry.png'
            if image.exists():
                original['text'] = 'Пробую✅'
                original['media'] = [{'path': str(image.relative_to(ROOT)), 'mime': 'image/png'}]
            payload = {'messages': [original], 'parents': []}
            result, trace = await agent.analyze(payload, -999, 1)
            output.append({'test': 'entry', 'result': result.model_dump(), 'trace': trace})
            assert any(s.action == 'open' and s.symbol == 'BRUSDT' and s.side == 'short'
                       and s.proposed_leverage() == 20 for s in result.signals), 'entry mismatch'
            close = {'id': 2, 'text': '75% закрываю✅', 'reply_to': 1, 'media': []}
            result, trace = await agent.analyze({'messages': [close], 'parents': [original]}, -999, 2)
            output.append({'test': 'partial_close', 'result': result.model_dump(), 'trace': trace})
            assert any(s.action == 'close_partial' and s.close_percent == '75'
                       and s.related_message_id == 1 for s in result.signals), 'close mismatch'
            (ROOT / 'data/smoke-results.json').write_text(
                json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
            print('LIVE SMOKE PASSED: entry and reply-based 75% close. No Telegram posts or trades created.')
    finally:
        store.db.close()


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except Exception as error:
        print('LIVE SMOKE FAILED:', type(error).__name__)
        sys.exit(1)
