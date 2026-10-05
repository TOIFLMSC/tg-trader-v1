"""One-off paper rehearsal against live public MEXC data.

It uses a temporary SQLite database and cannot place an exchange order.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

import httpx

from trader.mexc_market import MexcMarketClient
from trader.paper import PaperExecutor
from trader.store import Store


async def main():
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / 'paper-smoke.sqlite3')
        store.set_app_setting('execution_mode', 'paper', 'paper-smoke')
        channel = store.ensure_channel(-100123, 'Paper smoke')
        store.db.execute(
            "UPDATE channel_settings SET bank_limit_usdt='100' WHERE channel_id=?", (channel,))
        store.db.commit()
        event_time = datetime.now(timezone.utc).isoformat()
        event = store.enqueue(-100123, 1, {
            'channel_title': 'Paper smoke',
            'messages': [{'id': 1, 'date': event_time}],
        })
        store.complete(event, json.dumps({'summary': 'public paper smoke', 'signals': [{
            'action': 'open', 'symbol': 'AVAUSDT', 'side': 'short',
            'entry_kind': 'market', 'entry_prices': [],
            'leverage_min': '20', 'leverage_max': '20', 'stop_price': None,
            'take_profits': [], 'close_percent': None, 'reference_entry_price': None,
            'related_message_id': None, 'evidence': 'local smoke', 'questions': [],
        }]}))
        plan = store.db.execute('SELECT * FROM trade_plans').fetchone()
        async with httpx.AsyncClient(timeout=20) as http:
            market = MexcMarketClient(http)
            check = await market.check_plan(dict(plan), event_time)
            if check['status'] != 'valid':
                raise RuntimeError(f'Market check failed: {check["status"]}')
            store.save_market_check(plan['id'], check)
            store.review_plan(
                plan['id'], plan['version'], 'approve', {}, None, 'paper-smoke',
                'paper-public-smoke-approval')
            await PaperExecutor(store, market).step()
        position = store.db.execute('SELECT * FROM positions').fetchone()
        order = store.db.execute('SELECT * FROM paper_orders').fetchone()
        print(json.dumps({
            'plan_status': store.plan(plan['id'])['status'],
            'order_status': order['status'], 'symbol': position['symbol'],
            'entry': position['average_entry_price'],
            'contracts': position['remaining_quantity'],
            'margin_usdt': position['allocated_margin_usdt'],
            'realized_pnl_usdt': position['realized_pnl_usdt'],
        }, ensure_ascii=False))
        store.db.close()


if __name__ == '__main__':
    asyncio.run(main())
