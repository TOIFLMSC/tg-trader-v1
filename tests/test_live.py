import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from tests.helpers import valid_market_check
from trader.approvals import PlanReviewError
from trader.live import LiveExecutor, LiveReadiness
from trader.mexc_private import MexcPrivateError
from trader.store import Store


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


class FakeMarket:
    def __init__(self, last='0.2839'):
        self.last = last

    async def ticker(self, symbol):
        return {'symbol': symbol, 'lastPrice': self.last,
                'bid1': self.last, 'ask1': self.last}

    async def contract(self, symbol):
        return {'symbol': symbol, 'volUnit': '1'}


class FakePrivate:
    allow_orders = True

    def __init__(self):
        self.created = []
        self.leverages = []
        self.positions = []
        self.orders = []
        self.asset_rows = [{'currency': 'USDT', 'availableBalance': '100'}]
        self.mode = {'positionMode': 1}
        self.create_error = None
        self.open_orders_error = None

    async def assets(self):
        return self.asset_rows

    async def open_positions(self, symbol=None):
        return [row for row in self.positions if not symbol or row['symbol'] == symbol]

    async def open_orders(self, page_num=1, page_size=100):
        if self.open_orders_error:
            raise self.open_orders_error
        return self.orders

    async def position_mode(self):
        return self.mode

    async def change_isolated_leverage(self, symbol, position_type, leverage,
                                       position_id=None):
        self.leverages.append((symbol, position_type, leverage, position_id))
        return True

    async def create_order(self, payload):
        self.created.append(payload)
        if self.create_error:
            raise self.create_error
        return {'orderId': 'exchange-order-1'}

    async def order_by_external(self, symbol, external_oid):
        if self.create_error:
            raise MexcPrivateError('order not found')
        return {'orderId': 'exchange-order-1', 'externalOid': external_oid,
                'state': 3, 'dealVol': '352', 'dealAvgPrice': '0.2838'}

    async def place_tpsl(self, payload):
        return {'orderId': 'protection-1'}


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'live.sqlite3')
        self.channel = self.store.ensure_channel(-100123, 'Signals')
        self.store.db.execute(
            "UPDATE channel_settings SET bank_limit_usdt='100' WHERE channel_id=?",
            (self.channel,))
        self.store.set_app_setting('execution_mode', 'live', 'test')
        self.private = FakePrivate()
        self.market = FakeMarket()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def create_plan(self, signal=None, message=1, approve=True):
        signal = signal or opening()
        event = self.store.enqueue(-100123, message, {
            'channel_title': 'Signals', 'messages': [{
                'id': message, 'date': datetime.now(timezone.utc).isoformat()}],
        })
        self.store.complete(event, json.dumps({
            'summary': 'test', 'signals': [signal]}))
        plan = self.store.db.execute(
            'SELECT * FROM trade_plans WHERE event_id=?', (event,)).fetchone()
        if signal['action'] in ('open', 'add'):
            self.store.save_market_check(plan['id'], valid_market_check())
        if approve:
            self.arm()
            self.store.review_plan(plan['id'], plan['version'], 'approve', {}, None,
                                   'test', f'live-approval-{plan["id"]:04d}')
        return self.store.plan(plan['id'])

    def arm(self):
        self.store.set_app_setting('live_armed', 'true', 'test')

    def test_readiness_requires_empty_or_owned_isolated_account(self):
        readiness = LiveReadiness(self.store, self.private)
        snapshot_id = asyncio.run(readiness.check())
        row = self.store.db.execute(
            'SELECT * FROM live_snapshots WHERE id=?', (snapshot_id,)).fetchone()
        self.assertEqual((row['status'], row['usdt_available']), ('ready', '100'))

        self.private.positions = [{
            'positionId': 'foreign', 'symbol': 'BTC_USDT', 'openType': 1}]
        blocked = asyncio.run(readiness.check())
        row = self.store.db.execute(
            'SELECT status,reason FROM live_snapshots WHERE id=?', (blocked,)).fetchone()
        self.assertEqual(row['status'], 'blocked')
        self.assertIn('not owned', row['reason'])

    def test_live_plan_cannot_be_approved_while_latch_is_disarmed(self):
        plan = self.create_plan(approve=False)
        with self.assertRaisesRegex(PlanReviewError, 'защёлка снята'):
            self.store.review_plan(
                plan['id'], plan['version'], 'approve', {}, None, 'test',
                'live-disarmed-approval-0001')
        self.assertEqual(self.store.plan(plan['id'])['status'], 'ready')

    def test_read_only_transport_failure_disarms_and_requires_reapproval(self):
        plan = self.create_plan()
        self.private.open_orders_error = MexcPrivateError(
            'MEXC private request transport failure')
        executor = LiveExecutor(self.store, self.market, self.private)

        self.assertFalse(asyncio.run(executor.step()))

        current = self.store.plan(plan['id'])
        self.assertEqual(self.store.app_setting('live_armed'), 'false')
        self.assertEqual((current['status'], current['version'], current['reason_code']),
                         ('ready', 3, 'live_reapproval_required'))
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM live_orders').fetchone()[0], 0)

    def test_market_order_is_persisted_before_submission_and_reconciled(self):
        plan = self.create_plan()
        self.arm()
        executor = LiveExecutor(self.store, self.market, self.private)
        self.assertTrue(asyncio.run(executor.step()))  # persisted only
        self.assertEqual(len(self.private.created), 0)
        order = self.store.db.execute('SELECT * FROM live_orders').fetchone()
        self.assertEqual(order['status'], 'prepared')
        self.assertLessEqual(len(order['external_oid']), 32)

        self.assertTrue(asyncio.run(executor.step()))  # submitted
        order = self.store.db.execute('SELECT * FROM live_orders').fetchone()
        self.assertEqual(order['status'], 'open')
        self.assertEqual(len(self.private.created), 1)
        payload = self.private.created[0]
        self.assertEqual(payload['openType'], 1)
        self.assertEqual(payload['side'], 3)
        self.assertEqual(self.private.leverages, [('AVA_USDT', 2, 20, None)])

        self.private.positions = [{
            'positionId': 'position-1', 'symbol': 'AVA_USDT', 'positionType': 2,
            'openType': 1, 'holdVol': '352', 'holdAvgPrice': '0.2838',
            'leverage': 20, 'im': '5', 'unRealizedPnl': '0'}]
        self.assertTrue(asyncio.run(executor.step()))  # reconciled
        order = self.store.db.execute('SELECT * FROM live_orders').fetchone()
        position = self.store.db.execute('SELECT * FROM positions').fetchone()
        self.assertEqual(order['status'], 'filled')
        self.assertEqual((position['environment'], position['exchange_position_id']),
                         ('live', 'position-1'))
        self.assertEqual(self.store.plan(plan['id'])['status'], 'executed')

    def test_uncertain_submission_disarms_and_is_never_retried(self):
        self.create_plan()
        self.arm()
        self.private.create_error = MexcPrivateError('network', uncertain=True)
        executor = LiveExecutor(self.store, self.market, self.private)
        self.assertTrue(asyncio.run(executor.step()))
        self.assertTrue(asyncio.run(executor.step()))
        order = self.store.db.execute('SELECT * FROM live_orders').fetchone()
        self.assertEqual(order['status'], 'unknown')
        self.assertEqual(self.store.app_setting('live_armed'), 'false')
        self.assertEqual(len(self.private.created), 1)
        self.assertFalse(asyncio.run(executor.step()))
        self.assertEqual(len(self.private.created), 1)

    def test_trigger_limit_waits_for_crossing(self):
        self.create_plan(opening(entry_kind='trigger_limit', entry_prices=['0.28']))
        self.arm()
        self.market.last = '0.29'
        executor = LiveExecutor(self.store, self.market, self.private)
        self.assertTrue(asyncio.run(executor.step()))
        self.assertFalse(asyncio.run(executor.step()))
        self.assertEqual(len(self.private.created), 0)
        self.assertEqual(self.store.db.execute(
            'SELECT status FROM live_orders').fetchone()[0], 'pending_trigger')

    def test_partial_close_uses_exchange_position_and_reduce_only(self):
        self.create_plan()
        self.arm()
        executor = LiveExecutor(self.store, self.market, self.private)
        asyncio.run(executor.step())
        asyncio.run(executor.step())
        self.private.positions = [{
            'positionId': 'position-1', 'symbol': 'AVA_USDT', 'positionType': 2,
            'openType': 1, 'holdVol': '352', 'holdAvgPrice': '0.2838',
            'leverage': 20, 'im': '5', 'unRealizedPnl': '0'}]
        asyncio.run(executor.step())
        close = self.create_plan({
            'action': 'close_partial', 'symbol': 'AVAUSDT', 'side': 'short',
            'entry_kind': 'unspecified', 'entry_prices': [], 'leverage_min': None,
            'leverage_max': None, 'stop_price': None, 'take_profits': [],
            'close_percent': '75', 'reference_entry_price': '0.2888',
            'related_message_id': 1, 'evidence': 'close', 'questions': [],
        }, 2)
        self.assertEqual(close['status'], 'approved')
        self.assertTrue(asyncio.run(executor.step()))
        self.assertTrue(asyncio.run(executor.step()))
        payload = self.private.created[-1]
        self.assertEqual(payload['side'], 2)
        self.assertEqual(payload['positionId'], 'position-1')
        self.assertTrue(payload['reduceOnly'])
        self.assertEqual(payload['vol'], '264')


if __name__ == '__main__':
    unittest.main()
