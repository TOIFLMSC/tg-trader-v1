import tempfile
import unittest
from pathlib import Path
from pydantic import ValidationError

from trader.models import Signal, Analysis, render
from trader.correlation import AMBIGUOUS_LINK_QUESTION, correlate
from trader.service import owner_message
from trader.store import Store


def signal(**changes):
    values = dict(action='open', symbol='BRUSDT', side='short', entry_kind='market',
                  entry_prices=['0.89378'], leverage_min='20', leverage_max='20',
                  stop_price=None, take_profits=[], close_percent=None,
                  reference_entry_price=None,
                  related_message_id=None, evidence='Пробую', questions=[])
    values.update(changes)
    return Signal(**values)


class ModelTests(unittest.TestCase):
    def test_leverage_range_floors(self):
        self.assertEqual(signal(leverage_min='5', leverage_max='10').proposed_leverage(), 7)

    def test_numeric_units_rejected_with_field_location(self):
        for field, value in [('leverage_min', '20x'), ('leverage_max', '20X'),
                             ('close_percent', '75%'), ('stop_price', '0,2888')]:
            with self.subTest(field=field):
                with self.assertRaises(ValidationError) as caught:
                    signal(**{field: value})
                self.assertEqual(caught.exception.errors()[0]['loc'], (field,))

    def test_numeric_constraints_are_in_tool_schema(self):
        properties = Signal.model_json_schema()['properties']
        self.assertIn('pattern', properties['leverage_min']['anyOf'][0])
        self.assertIn('pattern', properties['entry_prices']['items'])

    def test_absent_leverage_not_automatically_assigned(self):
        s = signal(leverage_min=None, leverage_max=None)
        self.assertIsNone(s.proposed_leverage())
        card = render(Analysis(summary='Вход', signals=[s]), 'test', 1, None)
        self.assertIn('предложим 1×', card)

    def test_invalid_numbers_and_fields_fail_closed(self):
        for changes in ({'close_percent': '101'}, {'stop_price': 'NaN'},
                        {'entry_prices': ['-1']}, {'leverage_min': '20', 'leverage_max': '10'},
                        {'action': 'execute_shell'}, {'side': 'buy'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                signal(**changes)

    def test_partial_close_card_uses_remaining(self):
        s = signal(action='close_partial', close_percent='75', related_message_id=1)
        text = render(Analysis(summary='Закрытие', signals=[s]), 'test', 2, 1)
        self.assertIn('75% оставшегося', text)
        self.assertIn('Фактическое состояние и исполнение показаны в карточке плана', text)

    def test_management_card_shows_reference_entry(self):
        s = signal(action='close_partial', close_percent='50',
                   reference_entry_price='0.2888', related_message_id=2)
        text = render(Analysis(summary='Закрытие', signals=[s]), 'test', 3, None)
        self.assertIn('Цена исходного входа автора: 0.2888', text)

    def test_wrong_one_letter_ticker_is_corrected_by_trade_identity(self):
        close = signal(action='close_partial', symbol='AVAXUSDT', side='short',
                       entry_kind='unspecified', entry_prices=[], close_percent='50',
                       reference_entry_price='0.2888')
        candidates = [{'source_message_id': 2, 'symbol': 'AVAUSDT', 'side': 'short',
                       'entry_prices': ['0.2888'], 'leverage_min': '20', 'leverage_max': '20'}]
        result, notes = correlate(Analysis(summary='Закрытие AVAXUSDT', signals=[close]), candidates)
        self.assertEqual(result.signals[0].symbol, 'AVAUSDT')
        self.assertEqual(result.signals[0].related_message_id, 2)
        self.assertIn('AVAUSDT', result.summary)
        self.assertTrue(notes)

    def test_successful_correlation_removes_stale_ambiguity_question(self):
        close = signal(
            action='close_full', entry_kind='unspecified', entry_prices=[],
            symbol='AVAUSDT', side='short', reference_entry_price='0.2888',
            related_message_id=17, questions=[AMBIGUOUS_LINK_QUESTION])
        candidates = [{'source_message_id': 16, 'symbol': 'AVAUSDT', 'side': 'short',
                       'entry_prices': [], 'leverage_min': '20', 'leverage_max': '20'}]

        result, notes = correlate(Analysis(summary='Закрыт остаток', signals=[close]), candidates)

        self.assertEqual(result.signals[0].related_message_id, 16)
        self.assertEqual(result.signals[0].questions, [])
        self.assertTrue(notes)

    def test_ticker_similarity_alone_cannot_force_correlation(self):
        close = signal(action='close_partial', symbol='AVAXUSDT', side='short',
                       entry_kind='unspecified', entry_prices=[], close_percent='50',
                       leverage_min=None, leverage_max=None)
        candidates = [{'source_message_id': 2, 'symbol': 'AVAUSDT', 'side': 'short',
                       'entry_prices': ['0.2888'], 'leverage_min': '20', 'leverage_max': '20'}]
        result, notes = correlate(Analysis(summary='Закрытие AVAXUSDT', signals=[close]), candidates)
        self.assertEqual(result.signals[0].symbol, 'AVAXUSDT')
        self.assertIsNone(result.signals[0].related_message_id)
        self.assertFalse(notes)
        self.assertTrue(result.signals[0].questions)

    def test_bot_commands_require_owner_private_chat(self):
        update = {'message': {'from': {'id': 10}, 'chat': {'id': 10, 'type': 'private'}, 'text': '/pause'}}
        self.assertIsNotNone(owner_message(update, 10))
        self.assertIsNone(owner_message(update, 11))
        update['message']['chat']['type'] = 'group'
        self.assertIsNone(owner_message(update, 10))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_duplicate_and_edit(self):
        first = self.store.enqueue(1, 1, {'text': 'a'})
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.enqueue(1, 1, {'text': 'a'}))
        self.assertIsNotNone(self.store.enqueue(1, 1, {'text': 'b'}))

    def test_restart_and_outbox(self):
        event = self.store.enqueue(1, 1, {'text': 'a'})
        self.store.complete(event, Analysis(summary='test', signals=[]).model_dump_json())
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.outbox()['id'], event)
        self.store.notified(event)
        self.assertIsNone(self.store.outbox())

    def test_explicit_retry_only_requeues_failed_event(self):
        event = self.store.enqueue(1, 1, {'text': 'a'})
        self.assertFalse(self.store.retry_failed(event))
        self.store.fail(event, 'ValidationError')
        self.store.notified(event)
        self.assertTrue(self.store.retry_failed(event))
        self.assertEqual(self.store.next_pending()['id'], event)
        self.assertEqual(self.store.get(f'previous_error:{event}'), 'ValidationError')
        self.assertIsNone(self.store.outbox())

    def test_history_cannot_cross_channels_or_include_future(self):
        for ch, msg in [(1, 1), (2, 2), (1, 9)]:
            event = self.store.enqueue(ch, msg, {'text': 'a'})
            self.store.complete(event, '{"summary":"test","signals":[]}')
        self.assertEqual([h['message_id'] for h in self.store.history(1, 5)], [1])

    def test_active_candidate_comes_from_prior_open_only(self):
        opened = self.store.enqueue(1, 2, {'text': 'open'})
        self.store.complete(opened, Analysis(summary='open', signals=[signal()]).model_dump_json())
        other = self.store.enqueue(2, 1, {'text': 'other'})
        self.store.complete(other, Analysis(summary='other', signals=[signal(symbol='BTCUSDT')]).model_dump_json())
        candidates = self.store.active_candidates(1, 3)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['symbol'], 'BRUSDT')
        self.assertEqual(candidates[0]['source_message_id'], 2)

    def test_paper_candidates_come_from_actual_open_positions(self):
        self.store.set_app_setting('execution_mode', 'paper', 'test')
        opened = self.store.enqueue(1, 2, {
            'channel_title': 'Signals',
            'messages': [{'id': 2, 'date': '2026-10-03T12:00:00+00:00'}],
        })
        self.store.db.execute('''UPDATE channel_settings SET bank_limit_usdt='100'
            WHERE channel_id=(SELECT channel_id FROM events WHERE id=?)''', (opened,))
        self.store.db.commit()
        self.store.complete(opened, Analysis(
            summary='open', signals=[signal(symbol='AVAUSDT')]).model_dump_json())
        plan = self.store.db.execute(
            'SELECT * FROM trade_plans WHERE event_id=?', (opened,)).fetchone()
        self.store.db.execute('''INSERT INTO positions(
            environment,channel_id,opening_plan_id,symbol,side,leverage,status,
            initial_margin_usdt,remaining_quantity,created_at,updated_at)
            VALUES ('paper',?,?,?,'short',20,'open','5','25',?,?)''',
            (plan['channel_id'], plan['id'], 'AVAUSDT',
             '2026-10-03T12:00:00+00:00', '2026-10-03T12:00:00+00:00'))
        self.store.db.commit()

        candidates = self.store.active_candidates(1, 10)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['position_id'], 1)
        self.assertEqual(candidates[0]['source_message_id'], 2)

    def test_latest_edit_replaces_candidate_for_same_message(self):
        original = self.store.enqueue(1, 2, {'text': 'first'})
        self.store.complete(original, Analysis(
            summary='first', signals=[signal(symbol='BRUSDT')]).model_dump_json())
        edited = self.store.enqueue(1, 2, {'text': 'edited'})
        self.store.complete(edited, Analysis(
            summary='edited', signals=[signal(symbol='BTCUSDT')]).model_dump_json())
        candidates = self.store.active_candidates(1, 3)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['symbol'], 'BTCUSDT')

    def test_reanalysis_preserves_previous_result(self):
        event = self.store.enqueue(1, 2, {'text': 'a'})
        self.store.complete(event, '{"summary":"old","signals":[]}')
        self.store.notified(event)
        self.assertTrue(self.store.reanalyze(event, 'ticker correction'))
        self.assertEqual(self.store.next_pending()['id'], event)
        revision = self.store.db.execute(
            'SELECT reason,old_analysis FROM analysis_revisions WHERE event_id=?', (event,)).fetchone()
        self.assertEqual(revision['reason'], 'ticker correction')
        self.assertIn('old', revision['old_analysis'])

    def test_budget_reservation_survives_failure(self):
        self.store.reserve(0.25)
        with self.assertRaises(RuntimeError):
            self.store.reserve(0.25)
        self.assertEqual(self.store.spent(), 0.25)

    def test_settle_uses_token_usage(self):
        reservation = self.store.reserve(1)
        self.store.settle(reservation, {'input_tokens': 1000, 'output_tokens': 100})
        self.assertAlmostEqual(self.store.spent(), 0.00032)
