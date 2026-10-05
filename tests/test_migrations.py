import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trader.store import Store


class MigrationTests(unittest.TestCase):
    def test_legacy_event_is_preserved_and_channel_is_normalized(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY, channel INTEGER NOT NULL, message INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    analysis TEXT, error TEXT, notified INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(channel,message,fingerprint));
            ''')
            payload = json.dumps({
                'channel_title': 'BotTraderTest',
                'messages': [{'id': 7, 'date': '2026-09-20T12:00:00+00:00'}],
            })
            db.execute('INSERT INTO events(channel,message,fingerprint,payload) VALUES (?,?,?,?)',
                       (-100123, 7, 'fingerprint', payload))
            db.commit()
            db.close()

            store = Store(path)
            try:
                event = store.db.execute('SELECT * FROM events').fetchone()
                channel = store.db.execute('SELECT * FROM channels').fetchone()
                settings = store.db.execute('SELECT * FROM channel_settings').fetchone()
                versions = [row[0] for row in store.db.execute(
                    'SELECT version FROM schema_migrations ORDER BY version')]
                self.assertEqual(event['message'], 7)
                self.assertEqual(event['channel_id'], channel['id'])
                self.assertEqual(event['telegram_date'], '2026-09-20T12:00:00+00:00')
                self.assertEqual(channel['title'], 'BotTraderTest')
                self.assertEqual(settings['sizing_mode'], 'percent')
                self.assertEqual(settings['sizing_value'], '5')
                self.assertEqual(channel['connection_status'], 'unknown')
                self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
                tables = {row[0] for row in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({'trade_plans', 'positions', 'approvals',
                                 'plan_revisions', 'market_checks', 'paper_orders',
                                 'paper_fills', 'paper_notifications',
                                 'paper_funding', 'live_snapshots', 'live_orders',
                                 'live_notifications'} <= tables)
                position_columns = {row[1] for row in store.db.execute(
                    'PRAGMA table_info(positions)')}
                order_columns = {row[1] for row in store.db.execute(
                    'PRAGMA table_info(paper_orders)')}
                self.assertTrue({'funding_pnl_usdt', 'last_funding_at',
                                 'last_funding_check_at', 'recovery_status',
                                 'recovery_reason', 'recovery_candle_time',
                                 'recovery_options_json', 'exchange_position_id',
                                 'exchange_state_json', 'last_exchange_sync_at'} <= position_columns)
                fill_columns = {row[1] for row in store.db.execute(
                    'PRAGMA table_info(paper_fills)')}
                self.assertTrue({'version', 'cancelled_at', 'cancel_reason',
                                 'price_source', 'source_candle_time'} <= order_columns)
                self.assertTrue({'price_source', 'source_candle_time'} <= fill_columns)
                self.assertEqual(store.db.execute(
                    'SELECT version FROM trade_plans LIMIT 1').fetchone(), None)
            finally:
                store.db.close()

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.sqlite3'
            first = Store(path)
            first.db.close()
            second = Store(path)
            try:
                count = second.db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
                self.assertEqual(count, 11)
            finally:
                second.db.close()
