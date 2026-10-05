import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from trader.migrations import migrate, utc_now


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        migrate(self.db)
        self.db.commit()
        self.backfill_trade_plans()

    def backfill_trade_plans(self):
        """Project historical completed analyses as non-executable previews."""
        from trader.planner import sync_trade_plans

        rows = self.db.execute('''
            SELECT e.id,e.analysis FROM events e
            WHERE e.status='done' AND e.analysis IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM trade_plans p WHERE p.event_id=e.id)
            ORDER BY e.id''').fetchall()
        with self.db:
            for row in rows:
                try:
                    analysis = json.loads(row['analysis'])
                except (TypeError, json.JSONDecodeError):
                    continue
                sync_trade_plans(self.db, row['id'], analysis, environment_override='recognition')

    def ensure_channel(self, telegram_id, title=None):
        now = utc_now()
        title = str(title or f'Канал {telegram_id}')[:255]
        self.db.execute(
            'INSERT INTO channels(telegram_id,title,created_at,updated_at) VALUES (?,?,?,?) '
            'ON CONFLICT(telegram_id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at',
            (telegram_id, title, now, now))
        channel_id = self.db.execute('SELECT id FROM channels WHERE telegram_id=?', (telegram_id,)).fetchone()[0]
        self.db.execute('INSERT OR IGNORE INTO channel_settings(channel_id,updated_at) VALUES (?,?)',
                        (channel_id, now))
        self.db.commit()
        return channel_id

    def record_attempt(self, channel, message, step, tool, arguments):
        cur = self.db.execute('INSERT INTO agent_attempts(channel,message,step,tool,arguments) VALUES (?,?,?,?,?)',
                              (channel, message, step, tool, arguments))
        self.db.commit()
        return cur.lastrowid

    def record_validation(self, attempt, errors):
        self.db.execute('UPDATE agent_attempts SET validation_errors=? WHERE id=?',
                        (json.dumps(errors, ensure_ascii=False), attempt))
        self.db.commit()

    def get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))
        self.db.commit()

    def app_setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_app_setting(self, key, value, actor='recognition-service'):
        now = utc_now()
        with self.db:
            previous = self.db.execute(
                'SELECT value,version FROM app_settings WHERE key=?', (key,)).fetchone()
            before = {'value': previous['value'], 'version': previous['version']} if previous else None
            if previous and previous['value'] == str(value):
                return False
            if previous:
                version = previous['version'] + 1
                self.db.execute(
                    'UPDATE app_settings SET value=?,version=?,updated_at=? WHERE key=?',
                    (str(value), version, now, key))
            else:
                version = 1
                self.db.execute(
                    'INSERT INTO app_settings(key,value,version,updated_at) VALUES (?,?,?,?)',
                    (key, str(value), version, now))
            self.db.execute('''
                INSERT INTO audit_log(actor,action,entity_type,entity_id,before_json,after_json,created_at)
                VALUES (?,?,?,?,?,?,?)''',
                (actor, 'app_setting.updated', 'app_setting', key,
                 json.dumps(before, ensure_ascii=False, sort_keys=True) if before else None,
                 json.dumps({'value': str(value), 'version': version},
                            ensure_ascii=False, sort_keys=True), now))
            return True

    def paused(self):
        return self.app_setting('recognition_paused', 'false') == 'true'

    def set_paused(self, paused, actor='recognition-service'):
        value = 'true' if paused else 'false'
        return self.set_app_setting('recognition_paused', value, actor)

    def queue_control(self, kind, payload, idempotency_key, actor):
        now = utc_now()
        body = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self.db:
            current = self.db.execute(
                'SELECT id FROM control_commands WHERE idempotency_key=?',
                (idempotency_key,)).fetchone()
            if current:
                return current['id'], False
            cursor = self.db.execute('''
                INSERT INTO control_commands(kind,payload,idempotency_key,created_at)
                VALUES (?,?,?,?)''', (kind, body, idempotency_key, now))
            command_id = cursor.lastrowid
            self.db.execute('''
                INSERT INTO audit_log(actor,action,entity_type,entity_id,after_json,created_at)
                VALUES (?,?,?,?,?,?)''',
                (actor, 'control_command.requested', 'control_command', str(command_id),
                 json.dumps({'kind': kind, 'payload': payload or {}},
                            ensure_ascii=False, sort_keys=True), now))
            return command_id, True

    def next_control(self):
        return self.db.execute(
            "SELECT * FROM control_commands WHERE status='pending' ORDER BY id LIMIT 1").fetchone()

    def finish_control(self, command_id, status, error=None):
        if status not in ('applied', 'rejected', 'failed'):
            raise ValueError('Invalid control command status')
        now = utc_now()
        with self.db:
            command = self.db.execute(
                'SELECT kind,payload,status FROM control_commands WHERE id=?',
                (command_id,)).fetchone()
            if not command or command['status'] != 'pending':
                return False
            self.db.execute(
                'UPDATE control_commands SET status=?,applied_at=?,error=? WHERE id=?',
                (status, now, error, command_id))
            self.db.execute('''
                INSERT INTO audit_log(actor,action,entity_type,entity_id,after_json,created_at)
                VALUES (?,?,?,?,?,?)''',
                ('recognition-service', f'control_command.{status}', 'control_command',
                 str(command_id), json.dumps({'kind': command['kind'], 'error': error},
                                             ensure_ascii=False, sort_keys=True), now))
            return True

    def cancel_paper_order(self, order_id, expected_version, actor='recognition-service'):
        """Cancel one still-unfilled paper order with optimistic locking."""
        try:
            order_id = int(order_id)
            expected_version = int(expected_version)
        except (TypeError, ValueError):
            raise ValueError('Некорректный ID или версия paper-заявки') from None
        now = utc_now()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT * FROM paper_orders WHERE id=?', (order_id,)).fetchone()
            if not row:
                raise ValueError('Paper-заявка не найдена')
            if row['version'] != expected_version:
                raise ValueError('Paper-заявка уже изменилась; обновите страницу')
            if row['status'] not in ('pending_trigger', 'open'):
                raise ValueError('Отменить можно только ожидающую paper-заявку')
            before = dict(row)
            changed = self.db.execute('''UPDATE paper_orders
                SET status='cancelled',cancelled_at=?,cancel_reason='operator_cancelled',
                    updated_at=?,version=version+1
                WHERE id=? AND version=? AND status IN ('pending_trigger','open')''',
                (now, now, order_id, expected_version))
            if changed.rowcount != 1:
                raise ValueError('Paper-заявка изменилась до отмены; обновите страницу')
            if row['plan_id'] is not None:
                self.db.execute('''UPDATE trade_plans
                    SET status='cancelled',reason_code='paper_order_cancelled',
                        updated_at=?,version=version+1
                    WHERE id=? AND status='executing' ''', (now, row['plan_id']))
            after = dict(before)
            after.update(status='cancelled', cancelled_at=now,
                         cancel_reason='operator_cancelled', version=expected_version + 1)
            self.db.execute('''INSERT INTO audit_log(
                actor,action,entity_type,entity_id,before_json,after_json,created_at)
                VALUES (?,?,?,?,?,?,?)''', (
                actor, 'paper_order.cancelled', 'paper_order', str(order_id),
                json.dumps(before, ensure_ascii=False, sort_keys=True),
                json.dumps(after, ensure_ascii=False, sort_keys=True), now))
            self.db.execute('''INSERT OR IGNORE INTO paper_notifications(
                dedupe_key,text,created_at) VALUES (?,?,?)''', (
                f'paper-order-cancelled:{order_id}',
                f'PAPER · Заявка #{order_id} {row["symbol"]} отменена владельцем.', now))
            return after

    def resume_paper_position(self, position_id, expected_version,
                              actor='recognition-service'):
        """Acknowledge an ambiguous recovery candle and resume from now."""
        try:
            position_id = int(position_id)
            expected_version = int(expected_version)
        except (TypeError, ValueError):
            raise ValueError('Некорректный ID или версия paper-позиции') from None
        now = utc_now()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT * FROM positions WHERE id=?', (position_id,)).fetchone()
            if not row:
                raise ValueError('Paper-позиция не найдена')
            if row['version'] != expected_version:
                raise ValueError('Paper-позиция уже изменилась; обновите страницу')
            if row['status'] != 'open' or row['recovery_status'] != 'attention':
                raise ValueError('Позиция больше не ожидает решения по восстановлению')
            before = dict(row)
            changed = self.db.execute('''UPDATE positions SET recovery_status='ok',
                last_recovery_at=?,last_mark_at=?,updated_at=?,version=version+1
                WHERE id=? AND version=? AND status='open' AND recovery_status='attention' ''',
                (now, now, now, position_id, expected_version))
            if changed.rowcount != 1:
                raise ValueError('Paper-позиция изменилась до решения; обновите страницу')
            after = dict(before)
            after.update(recovery_status='ok', last_recovery_at=now, last_mark_at=now,
                         updated_at=now, version=expected_version + 1)
            self.db.execute('''INSERT INTO audit_log(
                actor,action,entity_type,entity_id,before_json,after_json,created_at)
                VALUES (?,?,?,?,?,?,?)''', (
                actor, 'paper_position.recovery_resumed', 'position', str(position_id),
                json.dumps(before, ensure_ascii=False, sort_keys=True),
                json.dumps(after, ensure_ascii=False, sort_keys=True), now))
            self.db.execute('''INSERT OR IGNORE INTO paper_notifications(
                dedupe_key,text,created_at) VALUES (?,?,?)''', (
                f'paper-recovery-resumed:{position_id}:{expected_version}',
                f'PAPER · {row["symbol"]}: неоднозначность подтверждена владельцем; '
                'наблюдение продолжено с текущей цены.', now))
            return after

    def enabled_channels(self):
        return self.db.execute(
            'SELECT id,telegram_id,title,connection_status FROM channels WHERE enabled=1 ORDER BY id'
        ).fetchall()

    def channel(self, channel_id):
        return self.db.execute(
            'SELECT id,telegram_id,title,enabled,connection_status FROM channels WHERE id=?',
            (channel_id,)).fetchone()

    def set_channel_enabled(self, channel_id, enabled):
        now = utc_now()
        status = 'unknown' if enabled else 'disabled'
        with self.db:
            cursor = self.db.execute(
                'UPDATE channels SET enabled=?,connection_status=?,last_error=NULL,checked_at=?,updated_at=? '
                'WHERE id=?', (1 if enabled else 0, status, now, now, channel_id))
        return bool(cursor.rowcount)

    def set_channel_connection(self, telegram_id, status, error=None):
        if status not in ('unknown', 'ready', 'error', 'disabled'):
            raise ValueError('Invalid channel connection status')
        now = utc_now()
        self.db.execute(
            'UPDATE channels SET connection_status=?,last_error=?,checked_at=?,updated_at=? '
            'WHERE telegram_id=?', (status, error, now, now, telegram_id))
        self.db.commit()

    def enqueue(self, channel, message, payload):
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        channel_id = self.ensure_channel(channel, payload.get('channel_title'))
        messages = payload.get('messages') or []
        telegram_date = messages[0].get('date') if messages else None
        cur = self.db.execute(
            'INSERT OR IGNORE INTO events(channel,message,fingerprint,payload,channel_id,telegram_date,received_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (channel, message, fingerprint, body, channel_id, telegram_date, utc_now()))
        self.db.commit()
        return cur.lastrowid if cur.rowcount else None

    def heartbeat(self, service_name, instance_id, status='running', details=None, started_at=None):
        now = utc_now()
        payload = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
        self.db.execute('''
            INSERT INTO service_heartbeats(
                service_name,instance_id,pid,status,details,started_at,updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(service_name) DO UPDATE SET
                instance_id=excluded.instance_id,pid=excluded.pid,status=excluded.status,
                details=excluded.details,
                started_at=CASE WHEN service_heartbeats.instance_id=excluded.instance_id
                    THEN service_heartbeats.started_at ELSE excluded.started_at END,
                updated_at=excluded.updated_at
            ''', (service_name, instance_id, os.getpid(), status, payload,
                  started_at or now, now))
        self.db.commit()

    def next_pending(self):
        return self.db.execute("SELECT * FROM events WHERE status='pending' ORDER BY id LIMIT 1").fetchone()

    def complete(self, event_id, result):
        from trader.planner import sync_trade_plans

        analysis = json.loads(result)
        with self.db:
            cursor = self.db.execute(
                "UPDATE events SET status='done',analysis=?,error=NULL WHERE id=?",
                (result, event_id))
            if not cursor.rowcount:
                raise ValueError('Event does not exist')
            sync_trade_plans(self.db, event_id, analysis)

    def fail(self, event_id, error):
        self.db.execute("UPDATE events SET status='failed',error=? WHERE id=?", (error, event_id))
        self.db.commit()

    def retry_failed(self, event_id):
        # Operator-only action; preserve the previous error before requeuing.
        row = self.db.execute("SELECT error FROM events WHERE id=? AND status='failed'", (event_id,)).fetchone()
        if row is None:
            return False
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                            (f'previous_error:{event_id}', row['error'] or 'unknown'))
            cur = self.db.execute("UPDATE events SET status='pending',error=NULL,notified=0 WHERE id=? AND status='failed'", (event_id,))
        return bool(cur.rowcount)

    def reanalyze(self, event_id, reason):
        row = self.db.execute('SELECT status,analysis,error FROM events WHERE id=?', (event_id,)).fetchone()
        if row is None or row['status'] not in ('done', 'failed'):
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute(
                'INSERT INTO analysis_revisions(event_id,reason,old_status,old_analysis,old_error,created_at) '
                'VALUES (?,?,?,?,?,?)',
                (event_id, reason, row['status'], row['analysis'], row['error'], now))
            cur = self.db.execute(
                "UPDATE events SET status='pending',analysis=NULL,error=NULL,notified=0 WHERE id=?",
                (event_id,))
            self.db.execute('''
                UPDATE trade_plans SET status='superseded',updated_at=?
                WHERE event_id=? AND status IN (
                    'preview','ready','needs_input','blocked','informational','approved')''',
                (now, event_id))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                            (f'reanalysis:{event_id}', reason))
        return bool(cur.rowcount)

    def outbox(self):
        return self.db.execute("SELECT * FROM events WHERE status IN ('done','failed') AND notified=0 ORDER BY id LIMIT 1").fetchone()

    def notified(self, event_id):
        self.db.execute('UPDATE events SET notified=1 WHERE id=?', (event_id,))
        self.db.commit()

    def history(self, channel, before):
        rows = self.db.execute('''
            WITH latest AS (
                SELECT message,MAX(id) AS id FROM events
                WHERE channel=? AND message<? AND status='done' GROUP BY message)
            SELECT e.message,e.analysis FROM events e JOIN latest l ON l.id=e.id
            ORDER BY e.message DESC LIMIT 8''', (channel, before)).fetchall()
        return [{'message_id': r['message'], 'analysis': json.loads(r['analysis'])} for r in rows]

    def active_candidates(self, channel, before):
        mode_row = self.db.execute(
            "SELECT value FROM app_settings WHERE key='execution_mode'").fetchone()
        mode = mode_row['value'] if mode_row else 'recognition'
        if mode in ('paper', 'live'):
            rows = self.db.execute('''
                SELECT p.id,p.symbol,p.side,p.leverage,op.source_message,
                       op.entry_prices_json
                FROM positions p JOIN channels c ON c.id=p.channel_id
                JOIN trade_plans op ON op.id=p.opening_plan_id
                WHERE c.telegram_id=? AND p.environment=?
                  AND p.status IN ('pending','open','closing')
                ORDER BY p.id''', (channel, mode)).fetchall()
            if rows:
                return [{
                    'position_id': row['id'],
                    'source_message_id': row['source_message'],
                    'symbol': row['symbol'], 'side': row['side'],
                    'entry_prices': json.loads(row['entry_prices_json'] or '[]'),
                    'leverage_min': str(row['leverage']) if row['leverage'] else None,
                    'leverage_max': str(row['leverage']) if row['leverage'] else None,
                } for row in rows]
        rows = self.db.execute('''
            WITH latest AS (
                SELECT message,MAX(id) AS id FROM events
                WHERE channel=? AND message<? AND status='done' GROUP BY message)
            SELECT e.message,e.analysis FROM events e JOIN latest l ON l.id=e.id
            ORDER BY e.message LIMIT 200''', (channel, before)).fetchall()
        candidates = []
        for row in rows:
            try:
                signals = json.loads(row['analysis']).get('signals', [])
            except (TypeError, json.JSONDecodeError):
                continue
            for signal in signals:
                action = signal.get('action')
                if action == 'open' and signal.get('symbol') and signal.get('side'):
                    candidates.append({
                        'source_message_id': row['message'],
                        'symbol': signal['symbol'], 'side': signal['side'],
                        'entry_prices': signal.get('entry_prices', []),
                        'leverage_min': signal.get('leverage_min'),
                        'leverage_max': signal.get('leverage_max'),
                    })
                elif action in ('close_full', 'cancel'):
                    linked = signal.get('related_message_id')
                    if linked is not None:
                        candidates = [c for c in candidates if c['source_message_id'] != linked]
                    elif signal.get('symbol'):
                        matches = [c for c in candidates if c['symbol'] == signal['symbol']
                                   and (not signal.get('side') or c['side'] == signal['side'])]
                        if len(matches) == 1:
                            candidates.remove(matches[0])
        return candidates[-20:]

    def reserve(self, budget, month=None):
        month = month or datetime.now(timezone.utc).strftime('%Y-%m')
        # A conservative charge per API attempt remains reserved on ambiguous failures.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            spent = self.db.execute('SELECT COALESCE(SUM(cost),0) FROM usage WHERE month=?', (month,)).fetchone()[0]
            if spent + 0.25 > budget:
                raise RuntimeError('Monthly application budget reached')
            return self.db.execute('INSERT INTO usage(month,cost) VALUES (?,0.25)', (month,)).lastrowid

    def settle(self, reservation, usage):
        if 'input_tokens' not in usage or 'output_tokens' not in usage:
            return
        # Only gpt-5.6-luna is enabled until rates for other models are configured.
        inp, out = int(usage['input_tokens']), int(usage['output_tokens'])
        cost = inp * 0.20 / 1_000_000 + out * 1.20 / 1_000_000
        self.db.execute('UPDATE usage SET cost=?,input_tokens=?,output_tokens=? WHERE id=?',
                        (cost, inp, out, reservation))
        self.db.commit()

    def stats(self):
        return {r[0]: r[1] for r in self.db.execute('SELECT status,COUNT(*) FROM events GROUP BY status')}

    def trade_stats(self):
        return {r[0]: r[1] for r in self.db.execute('''
            SELECT status,COUNT(*) FROM trade_plans
            WHERE status!='superseded' GROUP BY status''')}

    @staticmethod
    def _plan_market_sql(where):
        return f'''SELECT p.*,
            mc.status AS market_status,mc.reason AS market_reason,
            mc.mexc_symbol,mc.current_price,mc.bid_price,mc.ask_price,
            mc.estimated_contracts,mc.estimated_base_quantity,
            mc.estimated_notional_usdt,mc.observed_high,mc.observed_low,
            mc.signal_age_seconds,mc.checked_at AS market_checked_at,
            mc.contract_json AS market_contract_json,
            mc.ticker_json AS market_ticker_json,mc.candles_json AS market_candles_json
            FROM trade_plans p LEFT JOIN market_checks mc ON mc.id=p.market_check_id
            {where}'''

    def plans_for_event(self, event_id):
        return self.db.execute(self._plan_market_sql(
            "WHERE p.event_id=? AND p.status!='superseded' ORDER BY p.signal_index"),
            (event_id,)).fetchall()

    def plan(self, plan_id):
        return self.db.execute(
            self._plan_market_sql('WHERE p.id=?'), (plan_id,)).fetchone()

    def pending_reviews(self, limit=10):
        return self.db.execute(self._plan_market_sql('''
            WHERE p.status IN ('preview','ready','needs_input','blocked')
            ORDER BY p.id DESC LIMIT ?'''), (max(1, min(int(limit), 50)),)).fetchall()

    def next_market_plan(self, refresh_seconds=240):
        """Return one current opening plan whose public market check is due."""
        return self.db.execute(self._plan_market_sql('''
            JOIN events e ON e.id=p.event_id
            WHERE p.status IN ('preview','ready','needs_input','blocked','approved')
              AND p.action IN ('open','add') AND p.symbol IS NOT NULL
              AND (mc.id IS NULL OR (p.status!='approved' AND
                   (julianday('now')-julianday(mc.checked_at))*86400 > ?)
              )
            ORDER BY CASE WHEN mc.id IS NULL THEN 0 ELSE 1 END,p.id LIMIT 1''')
            .replace('SELECT p.*,', 'SELECT p.*,e.telegram_date,e.received_at,'),
            (max(30, int(refresh_seconds)),)).fetchone()

    def market_plan(self, plan_id):
        return self.db.execute(self._plan_market_sql('''
            JOIN events e ON e.id=p.event_id WHERE p.id=?''')
            .replace('SELECT p.*,', 'SELECT p.*,e.telegram_date,e.received_at,'),
            (plan_id,)).fetchone()

    def save_market_check(self, plan_id, check):
        now = check['checked_at']
        with self.db:
            current = self.db.execute(
                'SELECT id,status,market_check_id FROM trade_plans WHERE id=?',
                (plan_id,)).fetchone()
            if not current or current['status'] not in (
                    'preview', 'ready', 'needs_input', 'blocked', 'approved'):
                return None
            payload = (
                check['status'], check['reason'], check.get('mexc_symbol'),
                json.dumps(check.get('contract') or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(check.get('ticker') or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(check.get('candles') or {}, ensure_ascii=False, sort_keys=True),
                check.get('current_price'), check.get('bid_price'), check.get('ask_price'),
                check.get('estimated_contracts'), check.get('estimated_base_quantity'),
                check.get('estimated_notional_usdt'), check.get('observed_high'),
                check.get('observed_low'), check.get('signal_age_seconds'), now)
            if current['market_check_id']:
                check_id = current['market_check_id']
                self.db.execute('''UPDATE market_checks SET
                    status=?,reason=?,mexc_symbol=?,contract_json=?,ticker_json=?,candles_json=?,
                    current_price=?,bid_price=?,ask_price=?,estimated_contracts=?,
                    estimated_base_quantity=?,estimated_notional_usdt=?,observed_high=?,observed_low=?,
                    signal_age_seconds=?,checked_at=? WHERE id=? AND plan_id=?''',
                    (*payload, check_id, plan_id))
            else:
                cursor = self.db.execute('''
                    INSERT INTO market_checks(
                        status,reason,mexc_symbol,contract_json,ticker_json,candles_json,
                        current_price,bid_price,ask_price,estimated_contracts,
                        estimated_base_quantity,estimated_notional_usdt,observed_high,observed_low,
                        signal_age_seconds,checked_at,plan_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (*payload, plan_id))
                check_id = cursor.lastrowid
            self.db.execute(
                'UPDATE trade_plans SET market_check_id=?,updated_at=? WHERE id=?',
                (check_id, now, plan_id))
            return check_id

    def next_paper_notification(self):
        return self.db.execute(
            "SELECT * FROM paper_notifications WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()

    def paper_notification_sent(self, notification_id):
        with self.db:
            return bool(self.db.execute('''UPDATE paper_notifications
                SET status='sent',sent_at=? WHERE id=? AND status='pending' ''',
                (utc_now(), notification_id)).rowcount)

    def review_plan(self, plan_id, expected_version, decision, adjustments=None,
                    comment=None, actor='operator', idempotency_key=None):
        from trader.approvals import PlanReviewError, review_trade_plan

        if not idempotency_key or len(idempotency_key) < 12:
            raise PlanReviewError('Некорректный ключ операции')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            return review_trade_plan(
                self.db, int(plan_id), int(expected_version), decision, adjustments or {},
                comment, actor, idempotency_key)

    def spent(self):
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        return self.db.execute('SELECT COALESCE(SUM(cost),0) FROM usage WHERE month=?', (month,)).fetchone()[0]
