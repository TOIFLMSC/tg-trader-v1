import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from tests.helpers import valid_market_check
from trader.paper import PaperExecutor
from trader.store import Store


class FakeMarket:
    def __init__(self, last='0.2839', bid='0.2838', ask='0.2840'):
        self.set(last, bid, ask)
        self.funding_data = []
        self.candle_data = []
        self.candle_calls = []
        self.contract_data = {
            'symbol': 'AVA_USDT', 'positionOpenType': 3, 'futureType': 1,
            'contractSize': 1, 'minLeverage': 1, 'maxLeverage': 20,
            'countryConfigContractMaxLeverage': 0, 'priceUnit': '0.0001',
            'volUnit': 1, 'minVol': 1, 'maxVol': 3500, 'state': 0,
            'apiAllowed': True, 'settleCoin': 'USDT', 'quoteCoin': 'USDT',
            'takerFeeRate': '0.0004', 'maintenanceMarginRate': '0.005',
        }

    def set(self, last, bid, ask):
        self.ticker_data = {'symbol': 'AVA_USDT', 'lastPrice': last,
                            'bid1': bid, 'ask1': ask}

    async def contract(self, symbol):
        return self.contract_data if symbol == 'AVA_USDT' else None

    async def ticker(self, symbol):
        return dict(self.ticker_data)

    async def funding_history(self, symbol, page_size=100):
        return list(self.funding_data)

    async def candle_rows(self, symbol, start, end, interval='Min1'):
        self.candle_calls.append((symbol, start, end, interval))
        return list(self.candle_data)


def opening(**changes):
    value = {
        'action': 'open', 'symbol': 'AVAUSDT', 'side': 'short',
        'entry_kind': 'market', 'entry_prices': ['0.2888'],
        'leverage_min': '20', 'leverage_max': '20', 'stop_price': None,
        'take_profits': [], 'close_percent': None, 'reference_entry_price': None,
        'related_message_id': None, 'evidence': 'test', 'questions': [],
    }
    value.update(changes)
    return value


class PaperExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'paper.sqlite3')
        self.store.set_app_setting('execution_mode', 'paper', 'test')
        self.channel = self.store.ensure_channel(-100123, 'Signals')
        self.store.db.execute(
            "UPDATE channel_settings SET bank_limit_usdt='100' WHERE channel_id=?",
            (self.channel,))
        self.store.db.commit()
        self.market = FakeMarket()
        self.executor = PaperExecutor(self.store, self.market)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def create_plan(self, signal, message=1):
        event = self.store.enqueue(-100123, message, {
            'channel_title': 'Signals', 'messages': [{
                'id': message, 'date': datetime.now(timezone.utc).isoformat()}],
        })
        self.store.complete(event, json.dumps({'summary': 'test', 'signals': [signal]}))
        return self.store.db.execute(
            'SELECT * FROM trade_plans WHERE event_id=?', (event,)).fetchone()

    def approve(self, plan):
        if plan['action'] in ('open', 'add'):
            self.store.save_market_check(plan['id'], valid_market_check())
        self.store.review_plan(
            plan['id'], plan['version'], 'approve', {}, None, 'test',
            f'paper-approval-{plan["id"]:04d}')

    def test_market_open_and_partial_close_are_accounted(self):
        plan = self.create_plan(opening())
        self.assertEqual(plan['status'], 'ready')
        self.approve(plan)
        self.assertTrue(asyncio.run(self.executor.step()))

        position = self.store.db.execute('SELECT * FROM positions').fetchone()
        self.assertEqual((position['status'], position['side']), ('open', 'short'))
        self.assertEqual(position['remaining_quantity'], '352')
        self.assertEqual(position['average_entry_price'], '0.2838')
        self.assertLess(float(position['realized_pnl_usdt']), 0)
        self.assertEqual(self.store.plan(plan['id'])['status'], 'executed')

        close = self.create_plan({
            'action': 'close_partial', 'symbol': 'AVAUSDT', 'side': 'short',
            'entry_kind': 'unspecified', 'entry_prices': [], 'leverage_min': None,
            'leverage_max': None, 'stop_price': None, 'take_profits': [],
            'close_percent': '50', 'reference_entry_price': '0.2888',
            'related_message_id': 1, 'evidence': 'half', 'questions': [],
        }, 2)
        self.approve(close)
        self.assertTrue(asyncio.run(self.executor.step()))
        position = self.store.db.execute('SELECT * FROM positions').fetchone()
        self.assertEqual(position['remaining_quantity'], '176')
        self.assertEqual(position['allocated_margin_usdt'], '2.5')
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM paper_fills').fetchone()[0], 2)

    def test_trigger_limit_waits_for_trigger_and_limit_fill(self):
        self.market.set('0.29', '0.2899', '0.2901')
        plan = self.create_plan(opening(
            side='long', entry_kind='trigger_limit', entry_prices=['0.3']))
        self.approve(plan)
        asyncio.run(self.executor.step())
        order = self.store.db.execute('SELECT * FROM paper_orders').fetchone()
        self.assertEqual(order['status'], 'pending_trigger')
        self.assertEqual(self.store.plan(plan['id'])['status'], 'executing')

        self.market.set('0.301', '0.3009', '0.3011')
        asyncio.run(self.executor.step())
        self.assertEqual(self.store.db.execute(
            'SELECT status FROM paper_orders').fetchone()[0], 'open')
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM positions').fetchone()[0], 0)

        self.market.set('0.299', '0.2989', '0.2991')
        asyncio.run(self.executor.step())
        order = self.store.db.execute('SELECT * FROM paper_orders').fetchone()
        self.assertEqual((order['status'], order['average_fill_price']), ('filled', '0.3'))
        self.assertEqual(self.store.db.execute(
            'SELECT status FROM positions').fetchone()[0], 'open')

    def test_auto_mode_approves_only_valid_ready_plan(self):
        self.store.set_app_setting('approval_mode', 'auto', 'test')
        plan = self.create_plan(opening())
        self.store.save_market_check(plan['id'], valid_market_check())
        self.assertTrue(asyncio.run(self.executor.step()))
        self.assertEqual(self.store.plan(plan['id'])['status'], 'approved')
        self.assertTrue(asyncio.run(self.executor.step()))
        self.assertEqual(self.store.plan(plan['id'])['status'], 'executed')

    def test_stop_closes_position_without_second_approval(self):
        self.market.set('0.29', '0.2899', '0.2901')
        plan = self.create_plan(opening(
            side='long', stop_price='0.28', entry_prices=['0.29']))
        self.approve(plan)
        asyncio.run(self.executor.step())
        self.market.set('0.27', '0.2699', '0.2701')
        asyncio.run(self.executor.step())
        position = self.store.db.execute('SELECT * FROM positions').fetchone()
        self.assertEqual(position['status'], 'closed')
        self.assertEqual(self.store.db.execute(
            "SELECT intent FROM paper_orders ORDER BY id DESC LIMIT 1").fetchone()[0],
            'stop_loss')

    def test_approved_stop_update_changes_open_position(self):
        plan = self.create_plan(opening())
        self.approve(plan)
        asyncio.run(self.executor.step())
        stop = self.create_plan({
            'action': 'set_stop', 'symbol': 'AVAUSDT', 'side': 'short',
            'entry_kind': 'unspecified', 'entry_prices': [], 'leverage_min': None,
            'leverage_max': None, 'stop_price': '0.3', 'take_profits': [],
            'close_percent': None, 'reference_entry_price': '0.2888',
            'related_message_id': 1, 'evidence': 'set stop', 'questions': [],
        }, 2)
        self.approve(stop)
        asyncio.run(self.executor.step())
        position = self.store.db.execute('SELECT * FROM positions').fetchone()
        self.assertEqual(position['stop_price'], '0.3')
        order = self.store.db.execute(
            'SELECT intent,status FROM paper_orders ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(order), ('set_stop', 'filled'))

    def test_recognition_mode_freezes_execution(self):
        plan = self.create_plan(opening())
        self.approve(plan)
        self.store.set_app_setting('execution_mode', 'recognition', 'test')
        self.assertFalse(asyncio.run(self.executor.step()))
        self.assertEqual(self.store.plan(plan['id'])['status'], 'approved')
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM paper_orders').fetchone()[0], 0)

    def test_open_position_marking_is_throttled(self):
        plan = self.create_plan(opening())
        self.approve(plan)
        asyncio.run(self.executor.step())

        self.assertTrue(asyncio.run(self.executor.step()))
        first = self.store.db.execute(
            'SELECT version,last_mark_at FROM positions').fetchone()
        self.assertIsNotNone(first['last_mark_at'])
        self.assertFalse(asyncio.run(self.executor.step()))
        second = self.store.db.execute(
            'SELECT version,last_mark_at FROM positions').fetchone()
        self.assertEqual(tuple(first), tuple(second))

    def test_missing_taker_fee_fails_closed(self):
        self.market.contract_data.pop('takerFeeRate')
        plan = self.create_plan(opening())
        self.approve(plan)

        self.assertTrue(asyncio.run(self.executor.step()))
        self.assertEqual(self.store.plan(plan['id'])['status'], 'failed')
        order = self.store.db.execute(
            'SELECT status,error FROM paper_orders WHERE plan_id=?', (plan['id'],)).fetchone()
        self.assertEqual(order['status'], 'failed')
        self.assertIn('taker fee', order['error'])

    def test_trigger_order_failure_notifies_and_fails_plan(self):
        self.market.set('0.29', '0.2899', '0.2901')
        plan = self.create_plan(opening(
            side='long', entry_kind='trigger_limit', entry_prices=['0.3']))
        self.approve(plan)
        asyncio.run(self.executor.step())
        self.market.set(None, None, None)

        self.assertTrue(asyncio.run(self.executor.step()))
        order = self.store.db.execute('SELECT * FROM paper_orders').fetchone()
        self.assertEqual(order['status'], 'failed')
        self.assertEqual(self.store.plan(plan['id'])['status'], 'failed')
        notification = self.store.db.execute(
            "SELECT text FROM paper_notifications WHERE dedupe_key LIKE 'paper-order-failed:%'"
        ).fetchone()
        self.assertIn('не исполнен', notification['text'])

    def test_mark_failure_uses_worker_backoff(self):
        plan = self.create_plan(opening())
        self.approve(plan)
        asyncio.run(self.executor.step())
        self.market.contract_data.pop('maintenanceMarginRate')

        self.assertFalse(asyncio.run(self.executor.step()))

    def test_positive_funding_is_received_by_short_once(self):
        plan = self.create_plan(opening())
        self.approve(plan)
        asyncio.run(self.executor.step())
        position = self.store.db.execute('SELECT * FROM positions').fetchone()
        now = datetime.now(timezone.utc)
        opened = (now - timedelta(hours=2)).isoformat()
        settled = (now - timedelta(hours=1)).isoformat()
        self.store.db.execute('''UPDATE positions SET opened_at=?,created_at=?,
            last_mark_at=NULL,last_funding_at=NULL,last_funding_check_at=NULL WHERE id=?''',
                              (opened, opened, position['id']))
        self.store.db.commit()
        self.market.funding_data = [{
            'symbol': 'AVA_USDT', 'rate': '0.001', 'settle_time': settled,
        }]

        self.assertTrue(asyncio.run(self.executor.step()))
        updated = self.store.db.execute(
            'SELECT funding_pnl_usdt,realized_pnl_usdt FROM positions').fetchone()
        self.assertAlmostEqual(float(updated['funding_pnl_usdt']), 0.0999328, places=7)
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM paper_funding').fetchone()[0], 1)

        self.store.db.execute('''UPDATE positions SET last_mark_at=NULL,
            last_funding_check_at=NULL WHERE id=?''', (position['id'],))
        self.store.db.commit()
        asyncio.run(self.executor.step())
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM paper_funding').fetchone()[0], 1)

    def test_zero_funding_settlement_advances_checkpoint(self):
        plan = self.create_plan(opening())
        self.approve(plan)
        asyncio.run(self.executor.step())
        position = self.store.db.execute('SELECT * FROM positions').fetchone()
        now = datetime.now(timezone.utc)
        opened = (now - timedelta(hours=2)).isoformat()
        settled = (now - timedelta(hours=1)).isoformat()
        self.store.db.execute('''UPDATE positions SET opened_at=?,created_at=?,
            last_mark_at=NULL,last_funding_at=NULL,last_funding_check_at=NULL WHERE id=?''',
                              (opened, opened, position['id']))
        self.store.db.commit()
        self.market.funding_data = [{
            'symbol': 'AVA_USDT', 'rate': '0', 'settle_time': settled,
        }]

        self.assertTrue(asyncio.run(self.executor.step()))
        updated = self.store.db.execute(
            'SELECT funding_pnl_usdt,last_funding_at FROM positions').fetchone()
        self.assertEqual(updated['funding_pnl_usdt'], '0')
        self.assertEqual(updated['last_funding_at'], settled)
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM paper_funding').fetchone()[0], 1)

    def test_pending_trigger_order_can_be_cancelled_once(self):
        self.market.set('0.29', '0.2899', '0.2901')
        plan = self.create_plan(opening(
            side='long', entry_kind='trigger_limit', entry_prices=['0.3']))
        self.approve(plan)
        asyncio.run(self.executor.step())
        order = self.store.db.execute('SELECT * FROM paper_orders').fetchone()

        result = self.store.cancel_paper_order(order['id'], order['version'], 'test')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.store.plan(plan['id'])['status'], 'cancelled')
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='paper_order.cancelled'"
        ).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'изменилась'):
            self.store.cancel_paper_order(order['id'], order['version'], 'test')

    def _open_recovery_position(self):
        plan = self.create_plan(opening(
            side='long', stop_price='0.28', take_profits=['0.3'],
            entry_prices=['0.284']))
        self.approve(plan)
        asyncio.run(self.executor.step())
        position = self.store.db.execute('SELECT * FROM positions').fetchone()
        lower = datetime.now(timezone.utc) - timedelta(minutes=3)
        self.store.db.execute(
            'UPDATE positions SET last_mark_at=? WHERE id=?',
            (lower.isoformat(), position['id']))
        self.store.db.commit()
        return self.store.db.execute(
            'SELECT * FROM positions WHERE id=?', (position['id'],)).fetchone(), lower

    def test_restart_recovery_closes_at_single_missed_stop_level(self):
        position, lower = self._open_recovery_position()
        candle_time = int(lower.timestamp()) + 60
        self.market.set('0.29', '0.2899', '0.2901')
        self.market.candle_data = [{
            'time': candle_time, 'open': '0.285', 'high': '0.295',
            'low': '0.275', 'close': '0.29',
        }]

        self.assertTrue(asyncio.run(self.executor.step()))
        recovered = self.store.db.execute(
            'SELECT * FROM positions WHERE id=?', (position['id'],)).fetchone()
        self.assertEqual(recovered['status'], 'closed')
        order = self.store.db.execute(
            'SELECT * FROM paper_orders ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(
            (order['intent'], order['average_fill_price'], order['price_source']),
            ('stop_loss', '0.28', 'recovery_level'))
        self.assertEqual(
            order['source_candle_time'],
            datetime.fromtimestamp(candle_time, tz=timezone.utc).isoformat())
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='paper_position.recovered_exit'"
        ).fetchone()[0], 1)

    def test_ambiguous_recovery_requires_acknowledgement_and_pauses_protection(self):
        position, lower = self._open_recovery_position()
        candle_time = int(lower.timestamp()) + 60
        self.market.set('0.29', '0.2899', '0.2901')
        self.market.candle_data = [{
            'time': candle_time, 'open': '0.285', 'high': '0.31',
            'low': '0.275', 'close': '0.29',
        }]

        self.assertTrue(asyncio.run(self.executor.step()))
        attention = self.store.db.execute(
            'SELECT * FROM positions WHERE id=?', (position['id'],)).fetchone()
        self.assertEqual(
            (attention['status'], attention['recovery_status'], attention['recovery_reason']),
            ('open', 'attention', 'ambiguous_protection_order'))
        options = json.loads(attention['recovery_options_json'])
        self.assertEqual({item['intent'] for item in options}, {'stop_loss', 'take_profit'})
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM paper_fills').fetchone()[0], 1)

        self.market.set('0.27', '0.2699', '0.2701')
        asyncio.run(self.executor.mark_position(attention))
        still_open = self.store.db.execute(
            'SELECT * FROM positions WHERE id=?', (position['id'],)).fetchone()
        self.assertEqual((still_open['status'], still_open['recovery_status']),
                         ('open', 'attention'))

        resumed = self.store.resume_paper_position(
            position['id'], still_open['version'], 'test')
        self.assertEqual(resumed['recovery_status'], 'ok')
        with self.assertRaisesRegex(ValueError, 'изменилась'):
            self.store.resume_paper_position(position['id'], still_open['version'], 'test')
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='paper_position.recovery_resumed'"
        ).fetchone()[0], 1)

    def test_recovery_overlap_and_excessive_history_fail_to_attention(self):
        position, lower = self._open_recovery_position()
        self.market.candle_data = [{
            'time': int(lower.timestamp() // 60 * 60), 'open': '0.285',
            'high': '0.295', 'low': '0.275', 'close': '0.29',
        }]
        asyncio.run(self.executor.step())
        overlap = self.store.db.execute(
            'SELECT recovery_status,recovery_reason FROM positions WHERE id=?',
            (position['id'],)).fetchone()
        self.assertEqual(tuple(overlap), ('attention', 'overlapping_candle'))

        current = self.store.db.execute(
            'SELECT version FROM positions WHERE id=?', (position['id'],)).fetchone()
        self.store.resume_paper_position(position['id'], current['version'], 'test')
        old = (datetime.now(timezone.utc) - timedelta(minutes=2001)).isoformat()
        self.store.db.execute(
            "UPDATE positions SET last_mark_at=?,recovery_status='ok' WHERE id=?",
            (old, position['id']))
        self.store.db.commit()
        calls_before = len(self.market.candle_calls)
        asyncio.run(self.executor.step())
        excessive = self.store.db.execute(
            'SELECT recovery_status,recovery_reason FROM positions WHERE id=?',
            (position['id'],)).fetchone()
        self.assertEqual(tuple(excessive), ('attention', 'history_window_exceeded'))
        self.assertEqual(len(self.market.candle_calls), calls_before)

    def test_approved_plan_executes_once_after_store_restart(self):
        plan = self.create_plan(opening())
        self.approve(plan)
        path = Path(self.tmp.name) / 'paper.sqlite3'
        self.store.db.close()
        self.store = Store(path)
        restarted = PaperExecutor(self.store, self.market)

        self.assertTrue(asyncio.run(restarted.step()))
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM positions').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM paper_orders').fetchone()[0], 1)

        self.store.db.close()
        self.store = Store(path)
        restarted_again = PaperExecutor(self.store, self.market)
        asyncio.run(restarted_again.step())
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM positions').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM paper_orders').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
