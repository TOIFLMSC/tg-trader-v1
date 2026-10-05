import json
from pathlib import Path
import tempfile
import unittest

from trader.approvals import PlanReviewError
from trader.store import Store
from tests.helpers import valid_market_check


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


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'approvals.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def plan(self, value=None, bank='100', message=1):
        event = self.store.enqueue(-100123, message, {
            'channel_title': 'Signals',
            'messages': [{'id': message, 'date': '2026-10-03T12:00:00+00:00'}],
        })
        channel_id = self.store.db.execute(
            'SELECT channel_id FROM events WHERE id=?', (event,)).fetchone()[0]
        self.store.db.execute(
            'UPDATE channel_settings SET bank_limit_usdt=? WHERE channel_id=?',
            (bank, channel_id))
        self.store.db.commit()
        self.store.complete(event, json.dumps({
            'summary': 'test', 'signals': [value or signal()]}, ensure_ascii=False))
        plan = self.store.db.execute(
            'SELECT * FROM trade_plans WHERE event_id=?', (event,)).fetchone()
        self.store.save_market_check(plan['id'], valid_market_check())
        return self.store.db.execute(
            'SELECT * FROM trade_plans WHERE id=?', (plan['id'],)).fetchone()

    def test_approval_is_versioned_audited_and_idempotent(self):
        plan = self.plan()
        result, created = self.store.review_plan(
            plan['id'], 1, 'approve', {}, None, 'test', 'approval-idempotency-0001')
        repeated, repeated_created = self.store.review_plan(
            plan['id'], 1, 'approve', {}, None, 'test', 'approval-idempotency-0001')

        current = self.store.plan(plan['id'])
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(result['id'], repeated['id'])
        self.assertEqual((current['status'], current['version']), ('approved', 2))
        self.assertEqual(current['bank_limit_snapshot_usdt'], '100')
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM approvals').fetchone()[0], 1)
        self.assertEqual(tuple(self.store.db.execute(
            'SELECT old_version,new_version FROM plan_revisions').fetchone()), (1, 2))
        self.assertEqual(self.store.db.execute(
            'SELECT action FROM audit_log').fetchone()[0], 'plan.approved')

    def test_stale_plan_version_fails_closed(self):
        plan = self.plan()
        self.store.review_plan(
            plan['id'], 1, 'approve', {}, None, 'test', 'approval-idempotency-0002')
        with self.assertRaisesRegex(PlanReviewError, 'изменён'):
            self.store.review_plan(
                plan['id'], 1, 'reject', {}, None, 'test', 'approval-idempotency-0003')

    def test_adjustment_can_resolve_missing_fields_and_refreshes_bank_snapshot(self):
        plan = self.plan(signal(
            leverage_min=None, leverage_max=None,
            questions=['Укажите плечо']), bank=None)
        channel_id = plan['channel_id']
        self.store.db.execute(
            "UPDATE channel_settings SET bank_limit_usdt='80' WHERE channel_id=?",
            (channel_id,))
        self.store.db.commit()

        self.store.review_plan(
            plan['id'], 1, 'approve', {'leverage': '1', 'margin_usdt': '4'},
            'Используем 1x', 'test', 'approval-idempotency-0004')
        current = self.store.plan(plan['id'])
        self.assertEqual(current['effective_leverage'], 1)
        self.assertEqual(current['margin_usdt'], '4')
        self.assertEqual(current['questions_json'], '[]')
        self.assertEqual(current['bank_limit_snapshot_usdt'], '80')

    def test_unanswered_question_cannot_be_approved(self):
        plan = self.plan(signal(questions=['Нужно уточнение']))
        with self.assertRaisesRegex(PlanReviewError, 'Ответьте'):
            self.store.review_plan(
                plan['id'], 1, 'approve', {}, None, 'test', 'approval-idempotency-0005')
        self.assertEqual(self.store.plan(plan['id'])['status'], 'needs_input')

    def test_rejecting_approved_plan_cancels_it_without_execution(self):
        plan = self.plan()
        self.store.review_plan(
            plan['id'], 1, 'approve', {}, None, 'test', 'approval-idempotency-0006')
        self.store.review_plan(
            plan['id'], 2, 'reject', {}, 'cancel', 'test', 'approval-idempotency-0007')
        current = self.store.plan(plan['id'])
        self.assertEqual((current['status'], current['version'], current['reason_code']),
                         ('cancelled', 3, 'operator_rejected'))
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM positions').fetchone()[0], 0)

    def test_approved_plan_is_superseded_by_reanalysis(self):
        plan = self.plan()
        self.store.review_plan(
            plan['id'], 1, 'approve', {}, None, 'test', 'approval-idempotency-0008')
        self.assertTrue(self.store.reanalyze(plan['event_id'], 'correct source'))
        self.assertEqual(self.store.plan(plan['id'])['status'], 'superseded')


if __name__ == '__main__':
    unittest.main()
