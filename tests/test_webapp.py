import asyncio
import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from bootstrap import ROOT
from trader.store import Store
from trader.webapp import create_app
from tests.helpers import valid_market_check


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'web.sqlite3'
        self.media_path = ROOT / 'data' / 'media' / f'_web-test-{uuid.uuid4().hex}.png'
        self.media_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_path.write_bytes(b'not-a-real-png-but-safe-for-file-response')
        relative_media = self.media_path.relative_to(ROOT)
        payload = {
            'channel_title': 'BotTraderTest',
            'messages': [{
                'id': 11,
                'date': '2026-09-20T12:00:00+00:00',
                'text': 'Пробуем AVAUSDT',
                'media': [{
                    'path': str(relative_media),
                    'mime': 'text/html',
                    'sha256': hashlib.sha256(self.media_path.read_bytes()).hexdigest(),
                }],
            }],
        }
        store = Store(self.path)
        event = store.enqueue(-100123, 11, payload)
        analysis = {
            'summary': 'Открытие короткой позиции',
            'signals': [{
                'action': 'open', 'symbol': 'AVAUSDT', 'side': 'short',
                'entry_kind': 'market', 'entry_prices': ['0.2888'],
                'leverage_min': '20', 'leverage_max': '20', 'stop_price': None,
                'take_profits': [], 'close_percent': None, 'reference_entry_price': None,
                'related_message_id': None, 'evidence': 'Карточка позиции', 'questions': [],
            }],
        }
        channel_id = store.db.execute(
            'SELECT channel_id FROM events WHERE id=?', (event,)).fetchone()[0]
        store.db.execute(
            "UPDATE channel_settings SET bank_limit_usdt='100' WHERE channel_id=?",
            (channel_id,))
        store.db.commit()
        store.complete(event, json.dumps(analysis, ensure_ascii=False))
        plan_id = store.db.execute(
            'SELECT id FROM trade_plans WHERE event_id=?', (event,)).fetchone()[0]
        store.save_market_check(plan_id, valid_market_check())
        store.db.close()
        self.app = create_app(self.path)

    def tearDown(self):
        self.media_path.unlink(missing_ok=True)
        self.tmp.cleanup()

    def request(self, path):
        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                return await client.get(path)
        return asyncio.run(run())

    def test_dashboard_uses_normalized_data(self):
        response = self.request('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('BotTraderTest', response.text)
        self.assertIn('Recognition', response.text)
        self.assertNotIn('OPENAI_API_KEY', response.text)
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(response.headers['x-frame-options'], 'DENY')

    def test_health_endpoint(self):
        result = self.request('/healthz').json()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['schema_version'], 11)
        self.assertEqual(result['channels'], 1)
        self.assertEqual(result['events'], 1)

    def test_all_sections_and_post_detail_render(self):
        for path, marker in (
            ('/channels', 'Лимиты банка'),
            ('/posts', 'Оригинальные сообщения'),
            ('/posts/1', 'AVAUSDT'),
            ('/stats', 'Качество распознавания'),
            ('/trades', 'Торговые планы'),
            ('/control', 'Команды применяет reader'),
        ):
            response = self.request(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.text, path)
        trades = self.request('/trades').text
        self.assertIn('AVAUSDT', trades)
        self.assertIn('Предпросмотр', trades)
        self.assertIn('Торговые планы', self.request('/posts/1').text)

    def test_trades_and_stats_expose_polling_partials(self):
        trades = self.request('/trades')
        stats = self.request('/stats')
        trades_partial = self.request('/partials/trades')
        stats_partial = self.request('/partials/stats')

        self.assertIn('hx-get="/partials/trades?positions_page=1&amp;orders_page=1&amp;plans_page=1"', trades.text)
        self.assertIn('hx-trigger="every 3s"', trades.text)
        self.assertIn('data-pause-on-edit="true"', trades.text)
        self.assertIn('hx-get="/partials/stats?equity_period=days"', stats.text)
        self.assertIn('hx-trigger="every 10s"', stats.text)
        self.assertEqual(trades_partial.status_code, 200)
        self.assertIn('Торговые планы', trades_partial.text)
        self.assertNotIn('<html', trades_partial.text)
        self.assertEqual(stats_partial.status_code, 200)
        self.assertIn('Расход LLM', stats_partial.text)
        self.assertIn('Накопленный paper PnL', stats_partial.text)
        self.assertIn('Max drawdown', stats_partial.text)
        self.assertIn('Funding ledger', stats_partial.text)
        self.assertNotIn('<html', stats_partial.text)

    def test_every_polling_region_uses_global_edit_pause(self):
        for path, region_id in (
                ('/', 'dashboard-live'), ('/posts', 'posts-list'),
                ('/trades', 'trades-live'), ('/stats', 'stats-live'),
                ('/control', 'control-live')):
            page = self.request(path)
            self.assertIn(f'id="{region_id}" data-live-region', page.text, path)
            self.assertIn(f'data-live-status-for="{region_id}"', page.text, path)
        asset = self.request('/static/live-refresh.js')
        self.assertIn('editable(document.activeElement)', asset.text)
        self.assertIn('form[data-dirty="true"]', asset.text)

    def test_trade_sections_have_independent_ten_row_pagination(self):
        store = Store(self.path)
        try:
            store.set_app_setting('execution_mode', 'paper', 'test')
            channel_id = store.db.execute(
                'SELECT id FROM channels WHERE telegram_id=-100123').fetchone()[0]
            now = '2026-09-20T12:00:00+00:00'
            for index in range(12):
                position_id = store.db.execute('''INSERT INTO positions(
                    environment,channel_id,symbol,side,leverage,status,initial_margin_usdt,
                    allocated_margin_usdt,quantity,remaining_quantity,average_entry_price,
                    contract_size,opened_at,closed_at,created_at,updated_at)
                    VALUES ('paper',?,?,'short',2,'closed','5','0','10','0','1','1',
                            ?,?,?,?)''', (
                    channel_id, f'PAGE{index}USDT', now, now, now, now)).lastrowid
                store.db.execute('''INSERT INTO paper_orders(
                    position_id,client_key,symbol,side,intent,order_kind,status,
                    requested_contracts,filled_contracts,average_fill_price,
                    created_at,filled_at,updated_at)
                    VALUES (?,?,?,'short','open','market','filled','10','10','1',?,?,?)''', (
                    position_id, f'pagination-order-{index}', f'PAGE{index}USDT',
                    now, now, now))
            for index in range(2, 13):
                store.db.execute('''INSERT INTO trade_plans(
                    event_id,channel_id,signal_index,analysis_hash,revision,source_message,
                    action,symbol,side,order_kind,environment,status,signal_json,
                    created_at,updated_at)
                    VALUES (1,?,?,?,1,11,'report',?,'short','unspecified','recognition',
                            'informational','{}',?,?)''', (
                    channel_id, index, f'pagination-hash-{index}',
                    f'PAGE{index}USDT', now, now))
            store.db.commit()
        finally:
            store.db.close()

        page = self.request(
            '/trades?positions_page=2&orders_page=2&plans_page=2').text
        self.assertEqual(page.count('data-position-id='), 2)
        self.assertEqual(page.count('data-order-id='), 2)
        self.assertEqual(page.count('data-plan-id='), 2)
        self.assertEqual(page.count('страница 2 из 2'), 3)
        self.assertIn(
            'hx-get="/partials/trades?positions_page=2&amp;orders_page=2&amp;plans_page=2"',
            page)
        self.assertIn('positions_page=1&amp;orders_page=2&amp;plans_page=2', page)
        self.assertIn('positions_page=2&amp;orders_page=1&amp;plans_page=2', page)
        self.assertIn('positions_page=2&amp;orders_page=2&amp;plans_page=1', page)

    def test_virtual_orders_are_visible_only_in_paper_mode(self):
        self.assertNotIn('Виртуальные заявки', self.request('/partials/trades').text)
        store = Store(self.path)
        try:
            store.set_app_setting('execution_mode', 'paper', 'test')
        finally:
            store.db.close()
        self.assertIn('Виртуальные заявки', self.request('/partials/trades').text)

    def test_paper_stats_reconcile_fills_funding_and_position_pnl(self):
        store = Store(self.path)
        try:
            channel_id = store.db.execute(
                'SELECT id FROM channels WHERE telegram_id=-100123').fetchone()[0]
            now = datetime.now(timezone.utc).replace(microsecond=0)
            opened_at = (now - timedelta(days=2)).isoformat()
            settled_at = (now - timedelta(days=1)).isoformat()
            closed_at = now.isoformat()
            position_id = store.db.execute('''INSERT INTO positions(
                environment,channel_id,symbol,side,leverage,status,initial_margin_usdt,
                allocated_margin_usdt,quantity,remaining_quantity,average_entry_price,
                contract_size,realized_pnl_usdt,unrealized_pnl_usdt,entry_fees_usdt,
                exit_fees_usdt,funding_pnl_usdt,opened_at,closed_at,created_at,updated_at)
                VALUES ('paper',?,'AVAUSDT','short',20,'closed','5','0','100','0',
                        '0.2888','1','0.9','0','0.05','0.05','0.1',?,?,?,?)''', (
                channel_id, opened_at, closed_at, opened_at, closed_at)).lastrowid
            entry_order = store.db.execute('''INSERT INTO paper_orders(
                position_id,client_key,symbol,side,intent,order_kind,status,
                requested_contracts,filled_contracts,average_fill_price,created_at,
                filled_at,updated_at)
                VALUES (?,'stats-entry','AVAUSDT','short','open','market','filled',
                        '100','100','0.2888',?,?,?)''', (
                position_id, opened_at, opened_at, opened_at)).lastrowid
            close_order = store.db.execute('''INSERT INTO paper_orders(
                position_id,client_key,symbol,side,intent,order_kind,status,reduce_only,
                requested_contracts,filled_contracts,average_fill_price,created_at,
                filled_at,updated_at)
                VALUES (?,'stats-close','AVAUSDT','short','close_full','market','filled',1,
                        '100','100','0.28',?,?,?)''', (
                position_id, closed_at, closed_at, closed_at)).lastrowid
            store.db.execute('''INSERT INTO paper_fills(
                order_id,position_id,price,contracts,base_quantity,quote_notional_usdt,
                fee_usdt,realized_pnl_usdt,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)''', (
                entry_order, position_id, '0.2888', '100', '100', '28.88',
                '0.05', '0', opened_at))
            store.db.execute('''INSERT INTO paper_fills(
                order_id,position_id,price,contracts,base_quantity,quote_notional_usdt,
                fee_usdt,realized_pnl_usdt,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)''', (
                close_order, position_id, '0.28', '100', '100', '28',
                '0.05', '0.85', closed_at))
            store.db.execute('''INSERT INTO paper_funding(
                position_id,symbol,side,rate,position_value_usdt,amount_usdt,
                settle_time,price_source,created_at)
                VALUES (?,'AVAUSDT','short','0.0001','1000','0.1',
                        ?,'last_price_at_processing',?)''', (
                position_id, settled_at, settled_at))
            store.db.commit()
        finally:
            store.db.close()

        partial = self.request('/partials/stats')
        self.assertEqual(partial.status_code, 200)
        self.assertIn('<strong>0.9000</strong>', partial.text)
        self.assertIn('сейчас +0.9000 USDT', partial.text)
        self.assertIn('<strong>100.0%</strong>', partial.text)
        self.assertIn('+0.1000', partial.text)
        self.assertIn('last_price_at_processing', partial.text)
        self.assertIn('PnL, USDT', partial.text)
        self.assertIn('Дата', partial.text)
        self.assertIn('chart-axis-y', partial.text)
        self.assertIn('chart-axis-x', partial.text)

        months = self.request('/stats?equity_period=months')
        self.assertIn('<option value="months" selected>', months.text)
        self.assertIn('hx-get="/partials/stats?equity_period=months"', months.text)
        self.assertIn('Месяцы · последние 12', months.text)
        years = self.request('/partials/stats?equity_period=years')
        self.assertIn('<option value="years" selected>', years.text)
        self.assertIn('Годы · последние 10', years.text)
        fallback = self.request('/stats?equity_period=invalid')
        self.assertIn('<option value="days" selected>', fallback.text)

    def test_trades_partial_reflects_external_plan_decision(self):
        before = self.request('/partials/trades')
        self.assertIn('Предпросмотр', before.text)
        store = Store(self.path)
        try:
            store.review_plan(
                1, 1, 'approve', {}, None, 'telegram-test',
                'external-polling-test-0001')
        finally:
            store.db.close()
        after = self.request('/partials/trades')
        self.assertIn('Подтверждён', after.text)
        self.assertIn('План #1 · rev 1 · v2', after.text)

    def test_management_plan_without_market_check_can_be_approved(self):
        store = Store(self.path)
        try:
            channel_id = store.db.execute(
                'SELECT id FROM channels WHERE telegram_id=-100123').fetchone()[0]
            store.set_app_setting('execution_mode', 'paper', 'test')
            now = '2026-09-20T12:01:00+00:00'
            store.db.execute('''INSERT INTO positions(
                environment,channel_id,symbol,side,leverage,status,initial_margin_usdt,
                allocated_margin_usdt,quantity,remaining_quantity,average_entry_price,
                contract_size,opened_at,created_at,updated_at)
                VALUES ('paper',?,'AVAUSDT','short',20,'open','5','5','100','100',
                        '0.2888','1',?,?,?)''', (channel_id, now, now, now))
            store.db.commit()
            event = store.enqueue(-100123, 12, {
                'channel_title': 'BotTraderTest',
                'messages': [{'id': 12, 'date': now, 'text': 'закрываю 75%'}],
            })
            store.complete(event, json.dumps({
                'summary': 'Частичное закрытие',
                'signals': [{
                    'action': 'close_partial', 'symbol': 'AVAUSDT', 'side': 'short',
                    'entry_kind': 'unspecified', 'entry_prices': [],
                    'leverage_min': None, 'leverage_max': None, 'stop_price': None,
                    'take_profits': [], 'close_percent': '75',
                    'reference_entry_price': '0.2888', 'related_message_id': 11,
                    'evidence': 'закрываю 75%', 'questions': [],
                }],
            }, ensure_ascii=False))
            plan_id = store.db.execute(
                'SELECT id FROM trade_plans WHERE event_id=?', (event,)).fetchone()[0]
        finally:
            store.db.close()

        page = self.request('/trades').text
        form = re.search(
            rf'<form method="post" action="/plans/{plan_id}/review[^\"]*".*?</form>',
            page, re.DOTALL).group(0)
        self.assertIn('value="approve"', form)
        self.assertIn('Сохранить и подтвердить', form)

    def test_pending_paper_order_cancel_is_csrf_protected_and_queued(self):
        store = Store(self.path)
        try:
            store.set_app_setting('execution_mode', 'paper', 'test')
            now = '2026-09-20T12:02:00+00:00'
            store.db.execute(
                "UPDATE trade_plans SET environment='paper',status='executing' WHERE id=1")
            store.db.execute('''INSERT INTO paper_orders(
                plan_id,client_key,symbol,side,intent,order_kind,status,
                trigger_price,limit_price,requested_contracts,created_at,updated_at)
                VALUES (1,'web-cancel-order','AVAUSDT','short','open','trigger_limit',
                        'pending_trigger','0.28','0.28','100',?,?)''', (now, now))
            store.db.commit()
        finally:
            store.db.close()

        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url='http://testserver',
                    follow_redirects=False) as client:
                page = await client.get('/trades')
                form = re.search(
                    r'<form method="post" action="/paper-orders/1/cancel[^\"]*">.*?</form>',
                    page.text, re.DOTALL).group(0)
                csrf = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
                key = re.search(
                    r'name="idempotency_key" value="([^"]+)"', form).group(1)
                rejected = await client.post('/paper-orders/1/cancel', data={
                    'csrf': 'wrong', 'idempotency_key': key, 'expected_version': '1'})
                queued = await client.post(
                    '/paper-orders/1/cancel', headers={'Origin': 'http://testserver'}, data={
                        'csrf': csrf, 'idempotency_key': key, 'expected_version': '1'})
                return rejected, queued

        rejected, queued = asyncio.run(run())
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(queued.status_code, 303)
        db = sqlite3.connect(self.path)
        try:
            command = db.execute(
                "SELECT kind,payload,status FROM control_commands WHERE kind='paper_order.cancel'"
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(command[0], 'paper_order.cancel')
        self.assertIn('"order_id": 1', command[1])
        self.assertEqual(command[2], 'pending')

    def test_recovery_attention_is_visible_and_resume_is_versioned_and_queued(self):
        store = Store(self.path)
        try:
            channel_id = store.db.execute(
                'SELECT id FROM channels WHERE telegram_id=-100123').fetchone()[0]
            now = '2026-09-20T12:02:00+00:00'
            position_id = store.db.execute('''INSERT INTO positions(
                environment,channel_id,symbol,side,leverage,status,initial_margin_usdt,
                allocated_margin_usdt,quantity,remaining_quantity,average_entry_price,
                stop_price,take_profits_json,opened_at,created_at,updated_at,
                recovery_status,recovery_reason,recovery_candle_time,recovery_high,
                recovery_low,recovery_options_json)
                VALUES ('paper',?,'RECOVERYUSDT','long',2,'open','5','5','10','10','1',
                        '0.9','["1.1"]',?,?,?,'attention','ambiguous_protection_order',
                        ?,'1.2','0.8',?)''', (
                channel_id, now, now, now, now,
                json.dumps([{'intent': 'stop_loss', 'price': '0.9'},
                            {'intent': 'take_profit', 'price': '1.1'}]))).lastrowid
            store.db.commit()
        finally:
            store.db.close()

        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url='http://testserver',
                    follow_redirects=False) as client:
                page = await client.get('/trades')
                form = re.search(
                    rf'<form method="post" action="/positions/{position_id}/recovery/resume[^"]*".*?</form>',
                    page.text, re.DOTALL).group(0)
                csrf = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
                key = re.search(
                    r'name="idempotency_key" value="([^"]+)"', form).group(1)
                rejected = await client.post(
                    f'/positions/{position_id}/recovery/resume', data={
                        'csrf': 'wrong', 'idempotency_key': key,
                        'expected_version': '1'})
                queued = await client.post(
                    f'/positions/{position_id}/recovery/resume',
                    headers={'Origin': 'http://testserver'}, data={
                        'csrf': csrf, 'idempotency_key': key,
                        'expected_version': '1'})
                return page, rejected, queued

        page, rejected, queued = asyncio.run(run())
        self.assertIn('В одной минутной свече', page.text)
        self.assertIn('Продолжить с текущей цены', page.text)
        self.assertIn('stop_loss @ 0.9', page.text)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(queued.status_code, 303)
        db = sqlite3.connect(self.path)
        try:
            command = db.execute('''SELECT kind,payload,status FROM control_commands
                WHERE kind='paper_position.resume_recovery' ''').fetchone()
        finally:
            db.close()
        self.assertEqual(command[0], 'paper_position.resume_recovery')
        self.assertIn(f'"position_id": {position_id}', command[1])
        self.assertEqual(command[2], 'pending')

    def test_live_refresh_script_is_local_static_asset(self):
        page = self.request('/trades')
        asset = self.request('/static/live-refresh.js')
        self.assertIn('/static/live-refresh.js', page.text)
        self.assertEqual(asset.status_code, 200)
        self.assertIn('htmx:beforeRequest', asset.text)

    def test_posts_can_be_filtered(self):
        matching = self.request('/posts?action=open&symbol=AVA')
        missing = self.request('/posts?action=close')
        self.assertIn('AVAUSDT', matching.text)
        self.assertIn('Посты не найдены', missing.text)

    def test_media_is_served_only_from_valid_descriptor(self):
        response = self.request('/media/1/0')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.media_path.read_bytes())
        self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertEqual(self.request('/media/1/1').status_code, 404)

    def test_channel_settings_save_is_csrf_protected_and_audited(self):
        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url='http://testserver', follow_redirects=False) as client:
                page = await client.get('/channels')
                token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                rejected = await client.post('/channels/1', data={
                    'csrf': 'wrong', 'version': '1', 'bank_limit_usdt': '100',
                    'sizing_mode': 'percent', 'sizing_value': '5',
                })
                # Embedded browsers can omit Origin for an ordinary form
                # navigation.  The valid double-submit CSRF token still gates it.
                saved = await client.post('/channels/1', data={
                    'csrf': token, 'version': '1', 'bank_limit_usdt': '100',
                    'sizing_mode': 'percent', 'sizing_value': '7.5',
                })
                return rejected, saved

        rejected, saved = asyncio.run(run())
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(saved.status_code, 303)
        db = sqlite3.connect(self.path)
        try:
            settings = db.execute(
                'SELECT bank_limit_usdt,sizing_mode,sizing_value,version FROM channel_settings'
            ).fetchone()
            audit = db.execute('SELECT action FROM audit_log').fetchone()
        finally:
            db.close()
        self.assertEqual(settings, ('100', 'percent', '7.5', 2))
        self.assertEqual(audit[0], 'channel_settings.updated')

    def test_invalid_channel_settings_fail_closed(self):
        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                page = await client.get('/channels')
                token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                return await client.post('/channels/1', headers={'Origin': 'http://testserver'}, data={
                    'csrf': token, 'version': '1', 'bank_limit_usdt': '100',
                    'sizing_mode': 'percent', 'sizing_value': '101',
                })

        response = asyncio.run(run())
        self.assertEqual(response.status_code, 422)
        self.assertIn('100%', response.text)

    def test_plan_review_is_csrf_protected_versioned_and_audited(self):
        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url='http://testserver',
                    follow_redirects=False) as client:
                page = await client.get('/trades')
                form = re.search(
                    r'<form method="post" action="/plans/1/review[^\"]*".*?</form>',
                    page.text, re.DOTALL).group(0)
                csrf = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
                key = re.search(
                    r'name="idempotency_key" value="([^"]+)"', form).group(1)
                payload = {
                    'csrf': csrf, 'idempotency_key': key, 'expected_version': '1',
                    'decision': 'approve', 'leverage': '20', 'margin_usdt': '5',
                    'close_percent': '', 'comment': '',
                }
                rejected = await client.post(
                    '/plans/1/review', data=dict(payload, csrf='wrong'))
                approved = await client.post(
                    '/plans/1/review', headers={'Origin': 'http://testserver'}, data=payload)
                stale = dict(payload, idempotency_key='stale-plan-review-key-0001',
                             decision='reject')
                stale_response = await client.post(
                    '/plans/1/review', headers={'Origin': 'http://testserver'}, data=stale)
                return rejected, approved, stale_response

        rejected, approved, stale = asyncio.run(run())
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(approved.status_code, 303)
        self.assertEqual(stale.status_code, 409)
        db = sqlite3.connect(self.path)
        try:
            plan = db.execute(
                'SELECT status,version FROM trade_plans WHERE id=1').fetchone()
            approvals = db.execute('SELECT COUNT(*) FROM approvals').fetchone()[0]
            audit = db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='plan.approved'").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(plan, ('approved', 2))
        self.assertEqual((approvals, audit), (1, 1))

    def test_control_command_is_csrf_protected_and_idempotent(self):
        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url='http://testserver', follow_redirects=False) as client:
                page = await client.get('/control')
                csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                key = re.search(r'name="idempotency_key" value="([^"]+)"', page.text).group(1)
                originless = await client.post('/control/recognition', data={
                    'csrf': csrf, 'idempotency_key': key, 'action': 'pause'})
                bad_origin = await client.post('/control/recognition', headers={
                    'Origin': 'http://attacker.invalid'}, data={
                    'csrf': csrf, 'idempotency_key': key, 'action': 'pause'})
                first = await client.post('/control/recognition', headers={
                    'Origin': 'http://testserver'}, data={
                    'csrf': csrf, 'idempotency_key': key, 'action': 'pause'})
                second = await client.post('/control/recognition', headers={
                    'Origin': 'http://testserver'}, data={
                    'csrf': csrf, 'idempotency_key': key, 'action': 'pause'})
                return originless, bad_origin, first, second

        originless, bad_origin, first, second = asyncio.run(run())
        self.assertEqual(originless.status_code, 303)
        self.assertEqual(bad_origin.status_code, 403)
        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 303)
        db = sqlite3.connect(self.path)
        try:
            count = db.execute('SELECT COUNT(*) FROM control_commands').fetchone()[0]
            audit = db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='control_command.requested'"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 1)
        self.assertEqual(audit, 1)

    def test_paper_and_live_mode_requests_are_queued_for_reader_validation(self):
        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url='http://testserver',
                    follow_redirects=False) as client:
                page = await client.get('/control')
                form = re.search(
                    r'<form class="readiness[^>]*" method="post" action="/control/execution">.*?'
                    r'<input type="hidden" name="mode" value="paper">.*?</form>',
                    page.text, re.DOTALL).group(0)
                csrf = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
                key = re.search(
                    r'name="idempotency_key" value="([^"]+)"', form).group(1)
                paper = await client.post('/control/execution', headers={
                    'Origin': 'http://testserver'}, data={
                    'csrf': csrf, 'idempotency_key': key, 'mode': 'paper'})
                live = await client.post('/control/execution', headers={
                    'Origin': 'http://testserver'}, data={
                    'csrf': csrf, 'idempotency_key': key + '-live', 'mode': 'live'})
                return paper, live

        paper, live = asyncio.run(run())
        self.assertEqual(paper.status_code, 303)
        self.assertEqual(live.status_code, 303)
        db = sqlite3.connect(self.path)
        try:
            commands = db.execute(
                "SELECT kind,payload FROM control_commands WHERE kind='execution.set' ORDER BY id"
            ).fetchall()
        finally:
            db.close()
        self.assertEqual(len(commands), 2)
        self.assertIn('paper', commands[0][1])
        self.assertIn('live', commands[1][1])

    def test_loopback_origin_alias_is_accepted_but_wrong_port_is_rejected(self):
        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url='http://127.0.0.1:8787',
                    follow_redirects=False) as client:
                page = await client.get('/channels')
                csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                data = {
                    'csrf': csrf, 'version': '1', 'bank_limit_usdt': '100',
                    'sizing_mode': 'percent', 'sizing_value': '5',
                }
                alias = await client.post(
                    '/channels/1', headers={'Origin': 'http://localhost:8787'}, data=data)
                wrong_port = await client.post(
                    '/channels/1', headers={'Origin': 'http://localhost:9999'},
                    data=dict(data, version='2'))
                return alias, wrong_port

        alias, wrong_port = asyncio.run(run())
        self.assertEqual(alias.status_code, 303)
        self.assertEqual(wrong_port.status_code, 403)

    def test_add_channel_command_validates_telegram_id(self):
        async def run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url='http://testserver', follow_redirects=False) as client:
                page = await client.get('/channels')
                csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                key = re.search(r'name="idempotency_key" value="([^"]+)"', page.text).group(1)
                invalid = await client.post('/channel-actions/add', headers={
                    'Origin': 'http://testserver'}, data={
                    'csrf': csrf, 'idempotency_key': key, 'telegram_id': '123'})
                valid = await client.post('/channel-actions/add', headers={
                    'Origin': 'http://testserver'}, data={
                    'csrf': csrf, 'idempotency_key': key, 'telegram_id': '-1009876543210'})
                return invalid, valid

        invalid, valid = asyncio.run(run())
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(valid.status_code, 303)
        db = sqlite3.connect(self.path)
        try:
            command = db.execute('SELECT kind,payload,status FROM control_commands').fetchone()
        finally:
            db.close()
        self.assertEqual(command[0], 'channel.add')
        self.assertIn('-1009876543210', command[1])
        self.assertEqual(command[2], 'pending')

    def test_empty_channel_counts_are_zero(self):
        store = Store(self.path)
        store.ensure_channel(-100999, 'EmptyChannel')
        store.db.close()
        response = self.request('/channels')
        self.assertEqual(response.status_code, 200)
        self.assertIn('EmptyChannel', response.text)
        self.assertNotIn('>None<', response.text)

    def test_pending_command_counter_is_not_limited_by_table_page(self):
        store = Store(self.path)
        try:
            for index in range(21):
                store.queue_control('recognition.pause', {},
                                    f'pending-command-review-{index:02d}', 'test')
        finally:
            store.db.close()
        response = self.request('/control')
        self.assertEqual(response.status_code, 200)
        self.assertIn('ожидают: 21', response.text)
