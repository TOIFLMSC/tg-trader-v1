import asyncio
from datetime import datetime, timezone
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from types import SimpleNamespace

from trader.store import Store
from trader.service import Service, owner_callback, plan_keyboard, plan_review_text
from trader.models import Analysis
from tests.helpers import valid_market_check


class ControlStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'control.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_pause_state_and_command_lifecycle_are_audited(self):
        command_id, created = self.store.queue_control(
            'recognition.pause', {}, 'test-control-command-0001', 'test')
        repeated_id, repeated = self.store.queue_control(
            'recognition.pause', {}, 'test-control-command-0001', 'test')
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(command_id, repeated_id)
        self.assertEqual(self.store.next_control()['id'], command_id)

        self.assertTrue(self.store.set_paused(True))
        self.assertFalse(self.store.set_paused(True))
        self.assertTrue(self.store.paused())
        self.assertTrue(self.store.finish_control(command_id, 'applied'))
        self.assertIsNone(self.store.next_control())
        actions = [row[0] for row in self.store.db.execute(
            'SELECT action FROM audit_log ORDER BY id')]
        self.assertEqual(actions.count('control_command.requested'), 1)
        self.assertEqual(actions.count('app_setting.updated'), 1)
        self.assertIn('control_command.applied', actions)

    def test_channel_monitoring_state(self):
        channel_id = self.store.ensure_channel(-100123, 'Channel')
        self.assertTrue(self.store.set_channel_enabled(channel_id, False))
        row = self.store.channel(channel_id)
        self.assertEqual((row['enabled'], row['connection_status']), (0, 'disabled'))
        self.assertTrue(self.store.set_channel_enabled(channel_id, True))
        self.store.set_channel_connection(-100123, 'ready')
        row = self.store.channel(channel_id)
        self.assertEqual((row['enabled'], row['connection_status']), (1, 'ready'))

    def test_service_refreshes_dynamic_enabled_channel_set(self):
        first = self.store.ensure_channel(-100123, 'First')
        second = self.store.ensure_channel(-100456, 'Second')
        self.store.set_channel_enabled(second, False)
        service = Service({'TELEGRAM_OWNER_ID': '1', 'OPENAI_MODEL': 'gpt-5.6-luna'},
                          SimpleNamespace(), None,
                          self.store, 'test-instance')
        entity = SimpleNamespace(title='First')
        asyncio.run(service.refresh_channels({-100123: entity}))
        self.assertEqual(service.entities, {-100123: entity})
        self.assertEqual(self.store.channel(first)['connection_status'], 'ready')

        self.store.set_channel_enabled(first, False)
        asyncio.run(service.refresh_channels())
        self.assertEqual(service.entities, {})

    def test_service_applies_control_actions(self):
        class FakeTelegram:
            async def get_entity(self, telegram_id):
                return SimpleNamespace(title='Added', broadcast=True)

        service = Service({'TELEGRAM_OWNER_ID': '1', 'OPENAI_MODEL': 'gpt-5.6-luna'},
                          FakeTelegram(), None, self.store, 'test-instance')
        asyncio.run(service.apply_control({'kind': 'recognition.pause', 'payload': '{}'}))
        self.assertTrue(self.store.paused())
        asyncio.run(service.apply_control({
            'kind': 'approval.set', 'payload': '{"mode":"auto"}'}))
        self.assertEqual(self.store.app_setting('approval_mode'), 'auto')
        asyncio.run(service.apply_control({
            'kind': 'execution.set', 'payload': '{"mode":"paper"}'}))
        self.assertEqual(self.store.app_setting('execution_mode'), 'paper')
        with self.assertRaisesRegex(ValueError, 'live'):
            asyncio.run(service.apply_control({
                'kind': 'execution.set', 'payload': '{"mode":"live"}'}))

        with patch('trader.service.get_peer_id', return_value=-100789):
            asyncio.run(service.apply_control({
                'kind': 'channel.add', 'payload': '{"telegram_id":-100789}'}))
        channel = self.store.db.execute(
            'SELECT id,enabled,connection_status FROM channels WHERE telegram_id=-100789'
        ).fetchone()
        self.assertEqual((channel['enabled'], channel['connection_status']), (1, 'ready'))
        self.assertIn(-100789, service.entities)
        asyncio.run(service.apply_control({
            'kind': 'channel.monitor',
            'payload': f'{{"channel_id":{channel["id"]},"enabled":false}}'}))
        self.assertNotIn(-100789, service.entities)
        self.assertEqual(self.store.channel(channel['id'])['connection_status'], 'disabled')

        now = datetime.now(timezone.utc).isoformat()
        cursor = self.store.db.execute('''INSERT INTO paper_orders(
            client_key,symbol,side,intent,order_kind,status,created_at,updated_at)
            VALUES ('control-cancel-order','AVAUSDT','long','open','trigger_limit',
                    'pending_trigger',?,?)''', (now, now))
        self.store.db.commit()
        asyncio.run(service.apply_control({
            'kind': 'paper_order.cancel',
            'payload': json.dumps({'order_id': cursor.lastrowid, 'version': 1})}))
        cancelled = self.store.db.execute(
            'SELECT status,version FROM paper_orders WHERE id=?',
            (cursor.lastrowid,)).fetchone()
        self.assertEqual(tuple(cancelled), ('cancelled', 2))

        now = datetime.now(timezone.utc).isoformat()
        position_id = self.store.db.execute('''INSERT INTO positions(
            environment,channel_id,symbol,side,leverage,status,initial_margin_usdt,
            allocated_margin_usdt,quantity,remaining_quantity,average_entry_price,
            opened_at,created_at,updated_at,recovery_status,recovery_reason)
            VALUES ('paper',?,'AVAUSDT','long',2,'open','5','5','10','10','1',
                    ?,?,?,'attention','ambiguous_protection_order')''',
            (channel['id'], now, now, now)).lastrowid
        self.store.db.commit()
        asyncio.run(service.apply_control({
            'kind': 'paper_position.resume_recovery',
            'payload': json.dumps({'position_id': position_id, 'version': 1})}))
        resumed = self.store.db.execute(
            'SELECT recovery_status,version FROM positions WHERE id=?',
            (position_id,)).fetchone()
        self.assertEqual(tuple(resumed), ('ok', 2))

    def test_live_activation_requires_fresh_snapshot_and_exact_phrase(self):
        service = Service({'TELEGRAM_OWNER_ID': '1', 'OPENAI_MODEL': 'gpt-5.6-luna'},
                          SimpleNamespace(), None, self.store, 'test-instance')
        service.private = SimpleNamespace(allow_orders=True)
        now = datetime.now(timezone.utc).isoformat()
        snapshot_id = self.store.db.execute('''INSERT INTO live_snapshots(
            status,reason,usdt_available,position_mode,positions_json,orders_json,checked_at)
            VALUES ('ready','ok','100','1','[]','[]',?)''', (now,)).lastrowid
        self.store.db.commit()
        with self.assertRaisesRegex(ValueError, 'точную фразу'):
            asyncio.run(service.apply_control({
                'kind': 'live.activate', 'payload': '{"confirmation":"LIVE wrong"}'}))
        asyncio.run(service.apply_control({
            'kind': 'live.activate',
            'payload': json.dumps({'confirmation': f'LIVE {snapshot_id}'})}))
        self.assertEqual(self.store.app_setting('live_armed'), 'true')
        asyncio.run(service.apply_control({
            'kind': 'execution.set', 'payload': '{"mode":"live"}'}))
        self.assertEqual(self.store.app_setting('execution_mode'), 'live')
        asyncio.run(service.apply_control({
            'kind': 'execution.set', 'payload': '{"mode":"paper"}'}))
        self.assertEqual(self.store.app_setting('live_armed'), 'false')

    def test_service_ingests_once_and_processes_event(self):
        class FakeTelegram:
            async def get_messages(self, entity, ids):
                return None

        service = Service({'TELEGRAM_OWNER_ID': '1', 'OPENAI_MODEL': 'gpt-5.6-luna'},
                          FakeTelegram(), None, self.store, 'test-instance')
        message = SimpleNamespace(
            id=7, message='AVAUSDT short', date=datetime.now(timezone.utc),
            edit_date=None, reply_to_msg_id=None, grouped_id=None, post_author=None,
            photo=None, document=None, media=None, action=None)
        entity = SimpleNamespace(title='Signals')
        asyncio.run(service.ingest([message], -100777, entity))
        asyncio.run(service.ingest([message], -100777, entity))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)

        service.agent.analyze = AsyncMock(return_value=(Analysis(summary='ok', signals=[]), ['submit']))
        self.assertTrue(asyncio.run(service.process_next_event()))
        event = self.store.db.execute('SELECT status,analysis FROM events').fetchone()
        self.assertEqual(event['status'], 'done')
        self.assertIn('"summary":"ok"', event['analysis'])
        self.assertFalse(asyncio.run(service.process_next_event()))

    def test_service_fails_closed_and_respects_pause(self):
        event_id = self.store.enqueue(-100123, 1, {
            'channel_title': 'Signals',
            'messages': [{'id': 1, 'date': datetime.now(timezone.utc).isoformat()}],
        })
        service = Service({'TELEGRAM_OWNER_ID': '1', 'OPENAI_MODEL': 'gpt-5.6-luna'},
                          SimpleNamespace(), None, self.store, 'test-instance')
        service.agent.analyze = AsyncMock(side_effect=RuntimeError('secret detail'))
        self.store.set_paused(True)
        self.assertFalse(asyncio.run(service.process_next_event()))
        self.assertEqual(self.store.next_pending()['id'], event_id)
        self.store.set_paused(False)
        self.assertTrue(asyncio.run(service.process_next_event()))
        row = self.store.db.execute('SELECT status,error FROM events WHERE id=?',
                                    (event_id,)).fetchone()
        self.assertEqual((row['status'], row['error']), ('failed', 'RuntimeError'))

    def test_service_finishes_control_command(self):
        service = Service({'TELEGRAM_OWNER_ID': '1', 'OPENAI_MODEL': 'gpt-5.6-luna'},
                          SimpleNamespace(), None, self.store, 'test-instance')
        applied, _ = self.store.queue_control(
            'recognition.pause', {}, 'service-control-applied-01', 'test')
        self.assertTrue(asyncio.run(service.process_next_control()))
        row = self.store.db.execute(
            'SELECT status FROM control_commands WHERE id=?', (applied,)).fetchone()
        self.assertEqual(row['status'], 'applied')

        rejected, _ = self.store.queue_control(
            'approval.set', {'mode': 'invalid'}, 'service-control-rejected-01', 'test')
        self.assertTrue(asyncio.run(service.process_next_control()))
        row = self.store.db.execute(
            'SELECT status,error FROM control_commands WHERE id=?', (rejected,)).fetchone()
        self.assertEqual(row['status'], 'rejected')
        self.assertIn('режим', row['error'])
        self.assertFalse(asyncio.run(service.process_next_control()))

    def test_callback_security_and_versioned_plan_card(self):
        update = {'callback_query': {
            'id': 'callback-1', 'from': {'id': 1}, 'data': 'plan:7:3:approve',
            'message': {'chat': {'id': 1, 'type': 'private'}},
        }}
        self.assertIsNotNone(owner_callback(update, 1))
        self.assertIsNone(owner_callback(update, 2))
        update['callback_query']['message']['chat']['type'] = 'group'
        self.assertIsNone(owner_callback(update, 1))

        plan = {
            'id': 7, 'version': 3, 'status': 'preview', 'action': 'open',
            'symbol': 'AVAUSDT', 'side': 'short', 'environment': 'recognition',
            'order_kind': 'market', 'trigger_price': None, 'effective_leverage': 20,
            'margin_usdt': '5', 'close_percent': None, 'reason_code': None,
            'questions_json': '[]', 'market_status': 'valid',
        }
        self.assertIn('/approve 7 3', plan_review_text(plan))
        self.assertEqual(
            plan_keyboard(plan)['inline_keyboard'][0][0]['callback_data'],
            'plan:7:3:approve')

        close_plan = dict(
            plan, id=8, version=1, status='ready', action='close_partial',
            environment='paper', order_kind='unspecified', close_percent='75',
            market_status=None)
        keyboard = plan_keyboard(close_plan)
        self.assertEqual(keyboard['inline_keyboard'][0][0]['text'], '✅ Подтвердить')
        self.assertEqual(
            keyboard['inline_keyboard'][0][0]['callback_data'],
            'plan:8:1:approve')
        self.assertIn(
            'После подтверждения действие поступит в виртуальное исполнение.',
            plan_review_text(close_plan))

    def test_telegram_callback_approves_plan_without_execution(self):
        event = self.store.enqueue(-100123, 31, {
            'channel_title': 'Signals',
            'messages': [{'id': 31, 'date': '2026-10-03T12:00:00+00:00'}],
        })
        channel_id = self.store.db.execute(
            'SELECT channel_id FROM events WHERE id=?', (event,)).fetchone()[0]
        self.store.db.execute(
            "UPDATE channel_settings SET bank_limit_usdt='100' WHERE channel_id=?",
            (channel_id,))
        self.store.db.commit()
        self.store.complete(event, json.dumps({'summary': 'test', 'signals': [{
            'action': 'open', 'symbol': 'AVAUSDT', 'side': 'short',
            'entry_kind': 'market', 'entry_prices': ['0.2888'],
            'leverage_min': '20', 'leverage_max': '20', 'stop_price': None,
            'take_profits': [], 'close_percent': None,
            'reference_entry_price': '0.2888', 'related_message_id': None,
            'evidence': 'card', 'questions': [],
        }]}))
        plan = self.store.db.execute(
            'SELECT id,version FROM trade_plans WHERE event_id=?', (event,)).fetchone()
        self.store.save_market_check(plan['id'], valid_market_check())
        service = Service({
            'TELEGRAM_OWNER_ID': '1', 'OPENAI_MODEL': 'gpt-5.6-luna',
            'TELEGRAM_BOT_TOKEN': 'test-token',
        }, SimpleNamespace(), None, self.store, 'test-instance')
        update = {'update_id': 77, 'callback_query': {
            'id': 'callback-77', 'from': {'id': 1},
            'data': f'plan:{plan["id"]}:{plan["version"]}:approve',
            'message': {'chat': {'id': 1, 'type': 'private'}},
        }}
        with patch('trader.service.bot_call', new=AsyncMock(return_value=True)) as call:
            asyncio.run(service.process_bot_update(update))
        current = self.store.plan(plan['id'])
        self.assertEqual((current['status'], current['version']), ('approved', 2))
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM positions').fetchone()[0], 0)
        methods = [item.args[2] for item in call.await_args_list]
        self.assertIn('sendMessage', methods)
        self.assertIn('answerCallbackQuery', methods)
