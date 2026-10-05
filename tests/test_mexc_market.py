import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from trader.approvals import PlanReviewError
from trader.mexc_market import MexcMarketClient, normalize_symbol
from trader.store import Store


def contract(**changes):
    value = {
        'symbol': 'AVA_USDT', 'positionOpenType': 3, 'futureType': 1,
        'contractSize': 1, 'minLeverage': 1, 'maxLeverage': 20,
        'countryConfigContractMaxLeverage': 0, 'priceUnit': 0.0001,
        'volUnit': 1, 'minVol': 1, 'maxVol': 3500, 'state': 0,
        'apiAllowed': True, 'settleCoin': 'USDT', 'quoteCoin': 'USDT',
    }
    value.update(changes)
    return value


def ticker():
    return {'symbol': 'AVA_USDT', 'lastPrice': 0.2839, 'bid1': 0.2838,
            'ask1': 0.2840, 'timestamp': 1}


def plan(**changes):
    value = {
        'action': 'open', 'symbol': 'AVAUSDT', 'side': 'short',
        'order_kind': 'market', 'trigger_price': None, 'limit_price': None,
        'effective_leverage': 20, 'margin_usdt': '5', 'stop_price': None,
        'take_profits_json': '[]',
    }
    value.update(changes)
    return value


def transport(contract_data=None, ticker_data=None, kline_data=None, funding_data=None):
    contract_data = contract_data if contract_data is not None else contract()
    ticker_data = ticker_data if ticker_data is not None else ticker()

    def handler(request):
        path = request.url.path
        if '/funding_rate/history' in path:
            data = funding_data
        elif '/contract/detail' in path:
            data = contract_data
        elif path.endswith('/ticker'):
            data = ticker_data
        elif '/kline/' in path:
            data = kline_data
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={'success': True, 'code': 0, 'data': data})
    return httpx.MockTransport(handler)


class MexcMarketTests(unittest.TestCase):
    def run_check(self, value=None, event_time=None, now=None, **responses):
        async def run():
            async with httpx.AsyncClient(transport=transport(**responses)) as http:
                return await MexcMarketClient(http, 'https://test.mexc').check_plan(
                    value or plan(), event_time, now)
        return asyncio.run(run())

    def test_symbol_and_valid_order_estimate(self):
        self.assertEqual(normalize_symbol('avausdt'), 'AVA_USDT')
        result = self.run_check(
            event_time='2026-10-04T12:00:00+00:00',
            now=datetime(2026, 10, 4, 12, 5, tzinfo=timezone.utc))
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(result['mexc_symbol'], 'AVA_USDT')
        self.assertEqual(result['estimated_contracts'], '352')
        self.assertEqual(result['estimated_base_quantity'], '352')

    def test_funding_history_is_validated_and_sorted(self):
        history = {'resultList': [
            {'symbol': 'AVA_USDT', 'fundingRate': '-0.0002', 'settleTime': 2000},
            {'symbol': 'AVA_USDT', 'fundingRate': '0.0001', 'settleTime': 1000},
        ]}

        async def run():
            async with httpx.AsyncClient(
                    transport=transport(funding_data=history)) as http:
                return await MexcMarketClient(
                    http, 'https://test.mexc').funding_history('AVA_USDT')

        result = asyncio.run(run())
        self.assertEqual([item['rate'] for item in result], ['0.0001', '-0.0002'])
        self.assertLess(result[0]['settle_time'], result[1]['settle_time'])

    def test_candle_rows_are_validated_and_sorted(self):
        candles = {
            'time': [120, 60], 'open': ['2', '1'], 'high': ['3', '2'],
            'low': ['1', '0.5'], 'close': ['2.5', '1.5'],
        }

        async def run():
            async with httpx.AsyncClient(
                    transport=transport(kline_data=candles)) as http:
                return await MexcMarketClient(
                    http, 'https://test.mexc').candle_rows('AVA_USDT', 1, 180)

        result = asyncio.run(run())
        self.assertEqual([item['time'] for item in result], [60, 120])
        self.assertEqual(result[0]['low'], '0.5')

    def test_missing_contract_and_unsupported_leverage_fail_closed(self):
        missing = self.run_check(contract_data=[])
        self.assertEqual(missing['status'], 'contract_not_found')
        too_high = self.run_check(plan(effective_leverage=25))
        self.assertEqual(too_high['status'], 'leverage_unsupported')

    def test_trigger_price_must_match_price_step(self):
        result = self.run_check(plan(
            order_kind='trigger_limit', trigger_price='0.28885', limit_price='0.28885'))
        self.assertEqual(result['status'], 'price_step_mismatch')

    def test_custom_risk_tier_limits_leverage_for_volume(self):
        custom = contract(
            maxLeverage=100, riskLimitMode='CUSTOM', riskLimitCustom=[
                {'maxVol': 100, 'maxLeverage': 100},
                {'maxVol': 5000, 'maxLeverage': 10},
            ])
        result = self.run_check(plan(effective_leverage=20), contract_data=custom)
        self.assertEqual(result['status'], 'leverage_unsupported')

    def test_old_signal_detects_finished_scenario(self):
        now = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)
        result = self.run_check(
            plan(stop_price='0.30', take_profits_json='["0.25"]'),
            event_time=(now - timedelta(minutes=30)).isoformat(), now=now,
            kline_data={'time': [1, 2], 'high': [0.29, 0.301], 'low': [0.27, 0.26]})
        self.assertEqual(result['status'], 'scenario_finished')
        self.assertEqual((result['observed_low'], result['observed_high']), ('0.26', '0.301'))

    def test_old_signal_without_crossing_requires_review(self):
        now = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)
        result = self.run_check(
            event_time=(now - timedelta(minutes=30)).isoformat(), now=now,
            kline_data={'time': [1], 'high': [0.29], 'low': [0.27]})
        self.assertEqual(result['status'], 'stale_review')


class MarketStoreApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'market.sqlite3')
        event = self.store.enqueue(-100123, 1, {
            'channel_title': 'Signals',
            'messages': [{'id': 1, 'date': datetime.now(timezone.utc).isoformat()}],
        })
        channel = self.store.db.execute(
            'SELECT channel_id FROM events WHERE id=?', (event,)).fetchone()[0]
        self.store.db.execute(
            "UPDATE channel_settings SET bank_limit_usdt='100' WHERE channel_id=?", (channel,))
        self.store.db.commit()
        self.store.complete(event, json.dumps({'summary': 'test', 'signals': [{
            'action': 'open', 'symbol': 'AVAUSDT', 'side': 'short',
            'entry_kind': 'market', 'entry_prices': ['0.2888'],
            'leverage_min': '20', 'leverage_max': '20', 'stop_price': None,
            'take_profits': [], 'close_percent': None, 'reference_entry_price': '0.2888',
            'related_message_id': None, 'evidence': 'card', 'questions': [],
        }]}))
        self.plan = self.store.db.execute(
            'SELECT * FROM trade_plans WHERE event_id=?', (event,)).fetchone()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def save(self, status, reason='test'):
        check = {
            'status': status, 'reason': reason, 'mexc_symbol': 'AVA_USDT',
            'contract': contract(), 'ticker': ticker(), 'candles': {},
            'current_price': '0.2839', 'bid_price': '0.2838', 'ask_price': '0.284',
            'estimated_contracts': '352', 'estimated_base_quantity': '352',
            'estimated_notional_usdt': '99.8976', 'observed_high': None,
            'observed_low': None, 'signal_age_seconds': 60,
            'checked_at': datetime.now(timezone.utc).isoformat(),
        }
        self.store.save_market_check(self.plan['id'], check)

    def test_invalid_market_check_blocks_approval(self):
        self.save('api_disabled', 'API disabled')
        with self.assertRaisesRegex(PlanReviewError, 'запрещает вход'):
            self.store.review_plan(
                self.plan['id'], 1, 'approve', {}, None, 'test', 'market-blocked-0001')

    def test_missing_market_check_blocks_approval(self):
        with self.assertRaisesRegex(PlanReviewError, 'ещё не завершена'):
            self.store.review_plan(
                self.plan['id'], 1, 'approve', {}, None, 'test', 'market-missing-0001')

    def test_stale_market_check_requires_comment(self):
        self.save('stale_review', 'old')
        with self.assertRaisesRegex(PlanReviewError, 'добавьте комментарий'):
            self.store.review_plan(
                self.plan['id'], 1, 'approve', {}, None, 'test', 'market-stale-00001')
        self.store.review_plan(
            self.plan['id'], 1, 'approve', {}, 'Сценарий всё ещё актуален',
            'test', 'market-stale-00002')
        self.assertEqual(self.store.plan(self.plan['id'])['status'], 'approved')


if __name__ == '__main__':
    unittest.main()
