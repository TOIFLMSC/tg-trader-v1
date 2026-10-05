import json
from pathlib import Path
import tempfile
import unittest

from trader.store import Store
from trader.correlation import AMBIGUOUS_LINK_QUESTION


def signal(**changes):
    value = {
        'action': 'open', 'symbol': 'AVAUSDT', 'side': 'short',
        'entry_kind': 'market', 'entry_prices': ['0.2888'],
        'leverage_min': '20', 'leverage_max': '20',
        'stop_price': None, 'take_profits': [], 'close_percent': None,
        'reference_entry_price': '0.2888', 'related_message_id': None,
        'evidence': 'position card', 'questions': [],
    }
    value.update(changes)
    return value


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'planner.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def event(self, telegram_id=-100123, message=1, title='Signals'):
        event_id = self.store.enqueue(telegram_id, message, {
            'channel_title': title,
            'messages': [{'id': message, 'date': '2026-10-03T12:00:00+00:00'}],
        })
        channel_id = self.store.db.execute(
            'SELECT channel_id FROM events WHERE id=?', (event_id,)).fetchone()[0]
        self.store.db.execute('''
            UPDATE channel_settings SET bank_limit_usdt='100',sizing_mode='percent',
                sizing_value='5' WHERE channel_id=?''', (channel_id,))
        self.store.db.commit()
        return event_id

    def complete(self, event_id, signals):
        self.store.complete(event_id, json.dumps({'summary': 'test', 'signals': signals}))
        return self.store.db.execute(
            'SELECT * FROM trade_plans WHERE event_id=? ORDER BY id DESC LIMIT 1',
            (event_id,)).fetchone()

    def test_recognition_creates_non_executable_margin_preview(self):
        plan = self.complete(self.event(), [signal()])
        self.assertEqual(plan['status'], 'preview')
        self.assertEqual(plan['reason_code'], 'execution_disabled')
        self.assertEqual(plan['margin_usdt'], '5')
        self.assertEqual(plan['bank_limit_snapshot_usdt'], '100')
        self.assertEqual(plan['effective_leverage'], 20)
        self.assertEqual(plan['isolated'], 1)
        self.assertIsNone(plan['stop_price'])
        self.assertEqual(self.store.trade_stats(), {'preview': 1})

    def test_trigger_limit_uses_same_trigger_and_limit(self):
        plan = self.complete(self.event(), [signal(
            symbol='NEARUSDT', side='long', entry_kind='trigger_limit',
            entry_prices=['2.719'], leverage_min='5', leverage_max='10')])
        self.assertEqual((plan['trigger_price'], plan['limit_price']), ('2.719', '2.719'))
        self.assertEqual(plan['effective_leverage'], 7)

    def test_missing_leverage_or_bank_requires_input(self):
        event = self.event()
        self.store.db.execute(
            'UPDATE channel_settings SET bank_limit_usdt=NULL WHERE channel_id='
            '(SELECT channel_id FROM events WHERE id=?)', (event,))
        self.store.db.commit()
        plan = self.complete(event, [signal(leverage_min=None, leverage_max=None)])
        self.assertEqual(plan['status'], 'needs_input')
        questions = json.loads(plan['questions_json'])
        self.assertTrue(any('плечо' in item for item in questions))
        self.assertTrue(any('банк' in item for item in questions))

    def test_paper_ready_plan_reserves_symbol_for_first_channel(self):
        self.store.set_app_setting('execution_mode', 'paper', 'test')
        first = self.complete(self.event(message=1), [signal()])
        second = self.complete(self.event(-100456, 2, 'Other'), [signal()])
        self.assertEqual(first['status'], 'ready')
        self.assertEqual((second['status'], second['reason_code']),
                         ('blocked', 'symbol_reserved'))

    def test_ready_plans_cannot_exceed_fixed_channel_bank(self):
        self.store.set_app_setting('execution_mode', 'paper', 'test')
        first_event = self.event(message=1)
        channel_id = self.store.db.execute(
            'SELECT channel_id FROM events WHERE id=?', (first_event,)).fetchone()[0]
        self.store.db.execute('''
            UPDATE channel_settings SET sizing_mode='fixed_usdt',sizing_value='60'
            WHERE channel_id=?''', (channel_id,))
        self.store.db.commit()
        first = self.complete(first_event, [signal()])
        second_event = self.event(message=2)
        self.store.db.execute('''
            UPDATE channel_settings SET sizing_mode='fixed_usdt',sizing_value='60'
            WHERE channel_id=?''', (channel_id,))
        self.store.db.commit()
        second = self.complete(second_event, [signal(symbol='NEARUSDT')])
        self.assertEqual(first['status'], 'ready')
        self.assertEqual((second['status'], second['reason_code']),
                         ('blocked', 'bank_limit_exceeded'))

    def test_management_plan_targets_only_owned_position(self):
        self.store.set_app_setting('execution_mode', 'paper', 'test')
        event = self.event()
        channel_id = self.store.db.execute(
            'SELECT channel_id FROM events WHERE id=?', (event,)).fetchone()[0]
        self.store.db.execute('''
            INSERT INTO positions(environment,channel_id,symbol,side,leverage,status,
                initial_margin_usdt,created_at,updated_at)
            VALUES ('paper',?,'AVAUSDT','short',20,'open','5',
                    '2026-10-03T12:00:00+00:00','2026-10-03T12:00:00+00:00')''',
            (channel_id,))
        self.store.db.commit()
        plan = self.complete(event, [signal(
            action='close_partial', entry_kind='unspecified', entry_prices=[],
            leverage_min=None, leverage_max=None, close_percent='50')])
        self.assertEqual(plan['status'], 'ready')
        self.assertIsNotNone(plan['target_position_id'])

    def test_actual_paper_position_resolves_stale_history_ambiguity(self):
        self.store.set_app_setting('execution_mode', 'paper', 'test')
        event = self.event()
        channel_id = self.store.db.execute(
            'SELECT channel_id FROM events WHERE id=?', (event,)).fetchone()[0]
        self.store.db.execute('''
            INSERT INTO positions(environment,channel_id,symbol,side,leverage,status,
                initial_margin_usdt,remaining_quantity,created_at,updated_at)
            VALUES ('paper',?,'AVAUSDT','short',20,'open','5','25',
                    '2026-10-03T12:00:00+00:00','2026-10-03T12:00:00+00:00')''',
            (channel_id,))
        self.store.db.commit()

        plan = self.complete(event, [signal(
            action='close_full', entry_kind='unspecified', entry_prices=[],
            close_percent=None, related_message_id=17,
            questions=[AMBIGUOUS_LINK_QUESTION])])

        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(json.loads(plan['questions_json']), [])
        self.assertIsNotNone(plan['target_position_id'])

    def test_management_cannot_target_unopened_recognition(self):
        plan = self.complete(self.event(), [signal(
            action='close_partial', entry_kind='unspecified', entry_prices=[],
            leverage_min=None, leverage_max=None, close_percent='50')])
        self.assertEqual((plan['status'], plan['reason_code']),
                         ('blocked', 'no_open_position'))

    def test_same_analysis_is_idempotent_and_reanalysis_supersedes(self):
        event = self.event()
        analysis = json.dumps({'summary': 'first', 'signals': [signal()]})
        self.store.complete(event, analysis)
        self.store.complete(event, analysis)
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM trade_plans WHERE event_id=?', (event,)).fetchone()[0], 1)

        self.assertTrue(self.store.reanalyze(event, 'test revision'))
        revised = signal(symbol='NEARUSDT', side='long')
        self.store.complete(event, json.dumps({'summary': 'second', 'signals': [revised]}))
        plans = self.store.db.execute(
            'SELECT revision,status,symbol FROM trade_plans WHERE event_id=? ORDER BY revision',
            (event,)).fetchall()
        self.assertEqual([tuple(row) for row in plans], [
            (1, 'superseded', 'AVAUSDT'), (2, 'preview', 'NEARUSDT')])

    def test_restart_backfills_missing_historical_preview(self):
        event = self.event()
        self.store.complete(event, json.dumps({'summary': 'old', 'signals': [signal()]}))
        self.store.db.execute('DELETE FROM trade_plans WHERE event_id=?', (event,))
        self.store.db.commit()
        path = Path(self.tmp.name) / 'planner.sqlite3'
        self.store.db.close()
        self.store = Store(path)
        plan = self.store.db.execute(
            'SELECT environment,status FROM trade_plans WHERE event_id=?', (event,)).fetchone()
        self.assertEqual(tuple(plan), ('recognition', 'preview'))


if __name__ == '__main__':
    unittest.main()
