"""Fail-closed MEXC Live readiness, reconciliation and execution."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
import time

from trader.migrations import utc_now
from trader.mexc_market import normalize_symbol
from trader.mexc_private import MexcPrivateError


class LiveExecutionError(RuntimeError):
    pass


def _decimal(value, label='value', allow_zero=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise LiveExecutionError(f'Invalid {label}') from None
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise LiveExecutionError(f'Invalid {label}')
    return number


def _text(value):
    if value is None:
        return None
    text = format(Decimal(value).normalize(), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _position_mode_text(value):
    if isinstance(value, dict):
        value = value.get('positionMode', value.get('mode'))
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
        if isinstance(value, dict):
            value = value.get('positionMode', value.get('mode'))
    return str(value) if value is not None else 'unknown'


class LiveReadiness:
    def __init__(self, store, private):
        self.store = store
        self.private = private

    def _save(self, status, reason, available=None, mode=None,
              positions=None, orders=None):
        now = utc_now()
        with self.store.db:
            cursor = self.store.db.execute('''INSERT INTO live_snapshots(
                status,reason,usdt_available,position_mode,positions_json,orders_json,checked_at)
                VALUES (?,?,?,?,?,?,?)''', (
                status, reason, available, mode, _json(positions or []),
                _json(orders or []), now))
            self.store.db.execute('''INSERT INTO app_settings(key,value,updated_at)
                VALUES ('live_status',?,?) ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,updated_at=excluded.updated_at''', (status, now))
        return cursor.lastrowid

    def unavailable(self, reason='MEXC API credentials are not configured'):
        return self._save('blocked', reason)

    async def check(self):
        if self.private is None:
            return self.unavailable()
        try:
            assets = await self.private.assets()
            positions = await self.private.open_positions()
            orders = await self.private.open_orders()
            raw_mode = await self.private.position_mode()
            mode = _position_mode_text(raw_mode)
            usdt = next((item for item in assets
                         if str(item.get('currency', '')).upper() == 'USDT'), None)
            available = str((usdt or {}).get('availableBalance') or
                            (usdt or {}).get('availableOpen') or '0')
            if usdt is None:
                return self._save('blocked', 'USDT futures account asset was not returned',
                                  None, mode, positions, orders)
            if _decimal(available, 'available USDT', allow_zero=True) <= 0:
                return self._save('blocked', 'No available USDT futures balance',
                                  available, mode, positions, orders)
            if mode.strip().lower() not in ('1', 'hedge'):
                return self._save(
                    'blocked', 'Live MVP requires MEXC hedge position mode',
                    available, mode, positions, orders)
            known_positions = {str(row[0]) for row in self.store.db.execute('''
                SELECT exchange_position_id FROM positions WHERE environment='live'
                AND status IN ('pending','open','closing') AND exchange_position_id IS NOT NULL''')}
            foreign_positions = [item for item in positions
                                 if str(item.get('positionId')) not in known_positions]
            known_orders = {str(row[0]) for row in self.store.db.execute('''
                SELECT external_oid FROM live_orders WHERE status IN (
                'submitting','unknown','open','partially_filled','cancel_requested')''')}
            foreign_orders = [item for item in orders
                              if str(item.get('externalOid') or '') not in known_orders]
            if any(int(item.get('openType', 0)) != 1 for item in positions):
                return self._save('blocked', 'A non-isolated MEXC position exists',
                                  available, mode, positions, orders)
            if foreign_positions or foreign_orders:
                return self._save(
                    'blocked', 'MEXC has positions or orders not owned by this application',
                    available, mode, positions, orders)
            return self._save('ready', 'Private account checks passed',
                              available, mode, positions, orders)
        except (MexcPrivateError, LiveExecutionError) as error:
            return self._save('error', str(error)[:500])


class LiveExecutor:
    """One-step persisted state machine for approved Live plans.

    Every exchange order gets a deterministic ``externalOid`` before the first
    network write. Unknown submissions are queried and are never resubmitted.
    """
    ACTIVE_ORDER_STATUSES = (
        'prepared', 'pending_trigger', 'submitting', 'unknown', 'open',
        'partially_filled', 'cancel_requested')

    def __init__(self, store, market, private):
        self.store = store
        self.market = market
        self.private = private
        self._last_account_sync = 0.0
        self.account_sync_interval = 10

    @staticmethod
    def external_oid(plan_id):
        digest = hashlib.sha256(f'tg-trader-live-plan:{plan_id}'.encode()).hexdigest()[:12]
        return f'tgt-{plan_id}-{digest}'[:32]

    def _notify(self, db, key, text):
        db.execute('''INSERT OR IGNORE INTO live_notifications(
            dedupe_key,text,created_at) VALUES (?,?,?)''', (key, text, utc_now()))

    def _require_reapproval(self, reason):
        plans = self.store.db.execute('''SELECT * FROM trade_plans
            WHERE environment='live' AND status='approved' ORDER BY id''').fetchall()
        if not plans:
            return 0
        now = utc_now()
        with self.store.db:
            for plan in plans:
                before = {'status': plan['status'], 'version': plan['version'],
                          'reason_code': plan['reason_code']}
                new_version = plan['version'] + 1
                changed = self.store.db.execute('''UPDATE trade_plans
                    SET status='ready',reason_code='live_reapproval_required',
                        version=?,updated_at=?
                    WHERE id=? AND status='approved' AND version=?''',
                    (new_version, now, plan['id'], plan['version']))
                if not changed.rowcount:
                    continue
                self.store.db.execute('''INSERT INTO plan_revisions(
                    plan_id,old_version,new_version,reason,actor,before_json,created_at)
                    VALUES (?,?,?,?,?,?,?)''', (
                    plan['id'], plan['version'], new_version, 'live_disarmed',
                    'live-executor', _json(before), now))
                self.store.db.execute('''INSERT INTO audit_log(
                    actor,action,entity_type,entity_id,before_json,after_json,created_at)
                    VALUES ('live-executor','plan.reapproval_required','trade_plan',?,?,?,?)''', (
                    str(plan['id']), _json(before),
                    _json({'status': 'ready', 'version': new_version,
                           'reason_code': 'live_reapproval_required', 'reason': reason}), now))
                self._notify(
                    self.store.db, f'live-reapproval:{plan["id"]}:{new_version}',
                    f'LIVE · План #{plan["id"]} возвращён на подтверждение: защёлка снята.')
        return len(plans)

    def _disarm(self, reason):
        now = utc_now()
        with self.store.db:
            self.store.db.execute('''UPDATE app_settings SET value='false',version=version+1,
                updated_at=? WHERE key='live_armed' AND value!='false' ''', (now,))
            self.store.db.execute('''UPDATE app_settings SET value='blocked',version=version+1,
                updated_at=? WHERE key='live_status' ''', (now,))
            self.store.db.execute('''INSERT INTO audit_log(
                actor,action,entity_type,entity_id,after_json,created_at)
                VALUES ('live-executor','live.disarmed','app_setting','live_armed',?,?)''',
                (_json({'reason': reason}), now))
        self._require_reapproval(reason)

    def _plan(self):
        return self.store.db.execute('''SELECT p.*,mc.mexc_symbol,mc.contract_json,
            mc.ticker_json,mc.estimated_contracts FROM trade_plans p
            LEFT JOIN market_checks mc ON mc.id=p.market_check_id
            WHERE p.environment='live' AND p.status='approved'
            ORDER BY p.id LIMIT 1''').fetchone()

    def _active_order(self):
        marks = ','.join('?' for _ in self.ACTIVE_ORDER_STATUSES)
        return self.store.db.execute(f'''SELECT * FROM live_orders
            WHERE status IN ({marks}) ORDER BY id LIMIT 1''', self.ACTIVE_ORDER_STATUSES).fetchone()

    async def _prepare(self, plan):
        if not plan['symbol']:
            raise LiveExecutionError('Plan has no validated MEXC symbol')
        mexc_symbol = plan['mexc_symbol'] or normalize_symbol(plan['symbol'])
        position_id = plan['target_position_id']
        kind = ('protection' if plan['action'] in ('set_stop', 'set_take_profit')
                else ('trigger_limit' if plan['action'] in ('open', 'add')
                      and plan['order_kind'] == 'trigger_limit' else 'market'))
        status = 'pending_trigger' if kind == 'trigger_limit' else 'prepared'
        contracts = plan['estimated_contracts']
        position = None
        if plan['action'] in ('close_partial', 'close_full', 'set_stop', 'set_take_profit'):
            position = self.store.db.execute(
                "SELECT * FROM positions WHERE id=? AND environment='live' AND status='open'",
                (position_id,)).fetchone()
            if not position or not position['exchange_position_id']:
                raise LiveExecutionError('Live position is not reconciled')
            contracts = position['remaining_quantity']
            if plan['action'] == 'close_partial':
                contract = json.loads(plan['contract_json'] or '{}')
                if not contract:
                    contract = await self.market.contract(mexc_symbol)
                if not contract:
                    raise LiveExecutionError('MEXC contract is unavailable')
                unit = _decimal(contract.get('volUnit'), 'volume unit')
                percent = _decimal(plan['close_percent'], 'close percent')
                contracts = _text(((_decimal(contracts, 'position volume') * percent /
                                    Decimal(100)) / unit).to_integral_value(
                                        rounding=ROUND_FLOOR) * unit)
                if _decimal(contracts, 'close volume') <= 0:
                    raise LiveExecutionError('Close volume is below the contract step')
        now = utc_now()
        order_side = position['side'] if position is not None else plan['side']
        if order_side not in ('long', 'short'):
            raise LiveExecutionError('Plan side is not defined')
        with self.store.db:
            cursor = self.store.db.execute('''INSERT INTO live_orders(
                plan_id,position_id,external_oid,symbol,side,intent,order_kind,status,
                trigger_price,limit_price,requested_contracts,reduce_only,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                plan['id'], position_id, self.external_oid(plan['id']), mexc_symbol,
                order_side, plan['action'], kind, status, plan['trigger_price'],
                plan['limit_price'], contracts,
                1 if plan['action'].startswith('close_') else 0, now, now))
            self.store.db.execute(
                "UPDATE trade_plans SET status='executing',updated_at=? WHERE id=? AND status='approved'",
                (now, plan['id']))
            self._notify(self.store.db, f'live-prepared:{cursor.lastrowid}',
                         f'LIVE · План #{plan["id"]} подготовлен; реальная заявка ещё не отправлена.')
        return True

    async def _trigger_ready(self, order):
        if order['status'] != 'pending_trigger':
            return True
        ticker = await self.market.ticker(order['symbol'])
        last = _decimal(ticker.get('lastPrice'), 'last price')
        trigger = _decimal(order['trigger_price'], 'trigger')
        return last >= trigger if order['side'] == 'long' else last <= trigger

    def _position(self, order):
        if not order['position_id']:
            return None
        return self.store.db.execute('SELECT * FROM positions WHERE id=?',
                                     (order['position_id'],)).fetchone()

    def _order_payload(self, order, plan, position):
        opening = order['intent'] in ('open', 'add')
        if opening:
            side = 1 if order['side'] == 'long' else 3
        else:
            side = 4 if order['side'] == 'long' else 2
        is_limit = order['order_kind'] == 'trigger_limit'
        payload = {
            'symbol': order['symbol'],
            'price': order['limit_price'] if is_limit else '0',
            'vol': order['requested_contracts'],
            'side': side,
            'type': 1 if is_limit else 5,
            'openType': 1,
            'externalOid': order['external_oid'],
        }
        if opening:
            payload['leverage'] = int(plan['effective_leverage'])
        else:
            payload['positionId'] = str(position['exchange_position_id'])
            payload['reduceOnly'] = True
        return payload

    async def _submit_protection(self, order, plan, position):
        targets = json.loads(plan['take_profits_json'] or '[]')
        if order['intent'] == 'set_take_profit' and len(targets) != 1:
            raise LiveExecutionError('Live TP currently requires exactly one target per plan')
        payload = {
            'positionId': str(position['exchange_position_id']),
            'vol': order['requested_contracts'],
        }
        if order['intent'] == 'set_stop':
            payload.update(stopLossPrice=plan['stop_price'], lossTrend=1, stopLossType=1)
        else:
            payload.update(takeProfitPrice=str(targets[0]), profitTrend=1, takeProfitType=1)
        return payload, await self.private.place_tpsl(payload)

    async def _submit(self, order):
        plan = self.store.db.execute('SELECT * FROM trade_plans WHERE id=?',
                                     (order['plan_id'],)).fetchone()
        position = self._position(order)
        if order['order_kind'] == 'protection':
            payload = None
        else:
            payload = self._order_payload(order, plan, position)
        now = utc_now()
        with self.store.db:
            self.store.db.execute('''UPDATE live_orders SET status='submitting',request_json=?,
                submitted_at=?,updated_at=?,version=version+1 WHERE id=? AND status IN
                ('prepared','pending_trigger')''', (_json(payload) if payload else None,
                                                    now, now, order['id']))
        readiness_id = await LiveReadiness(self.store, self.private).check()
        readiness = self.store.db.execute(
            'SELECT * FROM live_snapshots WHERE id=?', (readiness_id,)).fetchone()
        if not readiness or readiness['status'] != 'ready':
            now = utc_now()
            reason = readiness['reason'] if readiness else 'MEXC readiness failed'
            with self.store.db:
                self.store.db.execute('''UPDATE live_orders SET status='attention',error=?,
                    updated_at=?,version=version+1 WHERE id=?''',
                    (str(reason)[:500], now, order['id']))
            self._disarm(str(reason))
            return True
        if order['intent'] in ('open', 'add'):
            try:
                await self.private.change_isolated_leverage(
                    order['symbol'], 1 if order['side'] == 'long' else 2,
                    plan['effective_leverage'],
                    position['exchange_position_id'] if order['intent'] == 'add'
                    and position is not None else None)
            except MexcPrivateError as error:
                now = utc_now()
                with self.store.db:
                    self.store.db.execute('''UPDATE live_orders SET status='attention',error=?,
                        updated_at=?,version=version+1 WHERE id=?''',
                        (f'Leverage change: {str(error)[:450]}', now, order['id']))
                self._disarm('Leverage change could not be confirmed')
                return True
        try:
            if order['order_kind'] == 'protection':
                payload, result = await self._submit_protection(order, plan, position)
            else:
                result = await self.private.create_order(payload)
            exchange_id = (result or {}).get('orderId') if isinstance(result, dict) else result
            final = 'filled' if order['order_kind'] == 'protection' else 'open'
            now = utc_now()
            with self.store.db:
                self.store.db.execute('''UPDATE live_orders SET status=?,exchange_order_id=?,
                    request_json=?,response_json=?,filled_at=?,updated_at=?,version=version+1
                    WHERE id=?''', (final, str(exchange_id) if exchange_id else None,
                    _json(payload), _json(result), now if final == 'filled' else None,
                    now, order['id']))
                if final == 'filled':
                    self.store.db.execute(
                        "UPDATE trade_plans SET status='executed',updated_at=? WHERE id=?",
                        (now, plan['id']))
                self._notify(
                    self.store.db, f'live-submitted:{order["id"]}:{final}',
                    f'LIVE · {order["symbol"]} {order["intent"]}: '
                    f'{"защитная команда принята" if final == "filled" else "заявка отправлена"}; '
                    f'exchange id {exchange_id or "не возвращён"}.')
            return True
        except MexcPrivateError as error:
            status = 'unknown' if error.uncertain and order['order_kind'] != 'protection' else 'attention'
            now = utc_now()
            with self.store.db:
                self.store.db.execute('''UPDATE live_orders SET status=?,error=?,updated_at=?,
                    version=version+1 WHERE id=?''', (status, str(error)[:500], now, order['id']))
                self._notify(
                    self.store.db, f'live-submit-{status}:{order["id"]}',
                    f'LIVE · {order["symbol"]}: результат отправки {status}; '
                    'повтор запрещён, защёлка снята, выполняется сверка.')
            self._disarm(str(error))
            return True
        except LiveExecutionError as error:
            now = utc_now()
            with self.store.db:
                self.store.db.execute('''UPDATE live_orders SET status='attention',error=?,
                    updated_at=?,version=version+1 WHERE id=?''',
                    (str(error)[:500], now, order['id']))
            self._disarm(str(error))
            return True

    async def _reconcile_order(self, order):
        try:
            data = await self.private.order_by_external(order['symbol'], order['external_oid'])
        except MexcPrivateError as error:
            if order['status'] == 'unknown':
                return False
            self._disarm(str(error))
            return False
        if not isinstance(data, dict):
            self._disarm('Invalid order reconciliation response')
            return False
        state = int(data.get('state', 0) or 0)
        status = {1: 'open', 2: 'open', 3: 'filled', 4: 'cancelled', 5: 'failed'}.get(
            state, 'attention')
        now = utc_now()
        with self.store.db:
            self.store.db.execute('''UPDATE live_orders SET status=?,exchange_order_id=?,
                filled_contracts=?,average_fill_price=?,response_json=?,filled_at=?,updated_at=?,
                version=version+1 WHERE id=?''', (
                status, str(data.get('orderId') or order['exchange_order_id'] or '') or None,
                str(data.get('dealVol') or data.get('filledVol') or '0'),
                str(data.get('dealAvgPrice') or data.get('avgPrice') or '') or None,
                _json(data), now if status == 'filled' else None, now, order['id']))
            if status == 'filled':
                self.store.db.execute(
                    "UPDATE trade_plans SET status='executed',updated_at=? WHERE id=?",
                    (now, order['plan_id']))
            if status in ('filled', 'cancelled', 'failed', 'attention'):
                self._notify(
                    self.store.db, f'live-order-state:{order["id"]}:{status}',
                    f'LIVE · {order["symbol"]} {order["intent"]}: статус {status}; '
                    f'исполнено {data.get("dealVol") or data.get("filledVol") or "0"}.')
        if status == 'filled':
            await self.sync_positions(order['symbol'])
        elif status in ('failed', 'attention'):
            self._disarm(f'Exchange order ended in {status}')
        return True

    async def sync_positions(self, symbol=None):
        rows = await self.private.open_positions(symbol)
        by_id = {str(item.get('positionId')): item for item in rows}
        now = utc_now()
        with self.store.db:
            local = self.store.db.execute('''SELECT * FROM positions WHERE environment='live'
                AND status IN ('pending','open','closing')''').fetchall()
            for position in local:
                remote = by_id.pop(str(position['exchange_position_id']), None)
                if remote is None:
                    if position['exchange_position_id']:
                        self.store.db.execute('''UPDATE positions SET status='closed',
                            remaining_quantity='0',closed_at=?,last_exchange_sync_at=?,updated_at=?,
                            version=version+1 WHERE id=?''', (now, now, now, position['id']))
                    continue
                if int(remote.get('openType', 0)) != 1:
                    raise LiveExecutionError('MEXC position is not isolated')
                self.store.db.execute('''UPDATE positions SET status='open',leverage=?,quantity=?,
                    remaining_quantity=?,average_entry_price=?,allocated_margin_usdt=?,
                    unrealized_pnl_usdt=?,exchange_state_json=?,last_exchange_sync_at=?,updated_at=?,
                    version=version+1 WHERE id=?''', (
                    int(remote.get('leverage') or position['leverage']),
                    str(remote.get('holdVol') or position['quantity']),
                    str(remote.get('holdVol') or position['remaining_quantity']),
                    str(remote.get('holdAvgPrice') or position['average_entry_price']),
                    str(remote.get('im') or position['allocated_margin_usdt']),
                    str(remote.get('unRealizedPnl') or '0'), _json(remote), now, now,
                    position['id']))
            # A newly filled opening order has no local position until its
            # exchange position id becomes visible here.
            for remote in by_id.values():
                if int(remote.get('openType', 0)) != 1:
                    raise LiveExecutionError('MEXC position is not isolated')
                remote_symbol = str(remote.get('symbol') or '')
                live_order = self.store.db.execute('''SELECT o.*,p.channel_id,p.effective_leverage,
                    p.margin_usdt,p.symbol AS local_symbol FROM live_orders o
                    JOIN trade_plans p ON p.id=o.plan_id WHERE o.symbol=? AND o.intent IN ('open','add')
                    AND o.status IN ('submitting','unknown','open','partially_filled','filled')
                    ORDER BY o.id DESC LIMIT 1''', (remote_symbol,)).fetchone()
                if not live_order:
                    raise LiveExecutionError('Unowned MEXC position appeared')
                local = self.store.db.execute('''SELECT * FROM positions WHERE environment='live'
                    AND symbol=? AND status IN ('pending','open','closing')''',
                    (live_order['local_symbol'],)).fetchone()
                if local:
                    self.store.db.execute('''UPDATE positions SET exchange_position_id=?,
                        exchange_state_json=?,last_exchange_sync_at=?,updated_at=? WHERE id=?''',
                        (str(remote.get('positionId')), _json(remote), now, now, local['id']))
                else:
                    side = 'long' if int(remote.get('positionType', 0)) == 1 else 'short'
                    self.store.db.execute('''INSERT INTO positions(
                        environment,channel_id,opening_plan_id,symbol,side,leverage,isolated,status,
                        initial_margin_usdt,allocated_margin_usdt,quantity,remaining_quantity,
                        average_entry_price,unrealized_pnl_usdt,opened_at,created_at,updated_at,
                        exchange_position_id,exchange_state_json,last_exchange_sync_at)
                        VALUES ('live',?,?,?,?,?,1,'open',?,?,?,?,?,?,?,?,?,?,?,?)''', (
                        live_order['channel_id'], live_order['plan_id'], live_order['local_symbol'],
                        side, int(remote.get('leverage') or live_order['effective_leverage']),
                        str(live_order['margin_usdt']), str(remote.get('im') or live_order['margin_usdt']),
                        str(remote.get('holdVol')), str(remote.get('holdVol')),
                        str(remote.get('holdAvgPrice')), str(remote.get('unRealizedPnl') or '0'),
                        now, now, now, str(remote.get('positionId')), _json(remote), now))
        return True

    async def cancel_order(self, order_id, expected_version):
        order = self.store.db.execute(
            'SELECT * FROM live_orders WHERE id=?', (order_id,)).fetchone()
        if not order or order['version'] != expected_version:
            raise ValueError('Live-заявка уже изменилась; обновите страницу')
        if order['status'] in ('prepared', 'pending_trigger'):
            now = utc_now()
            with self.store.db:
                changed = self.store.db.execute('''UPDATE live_orders SET status='cancelled',
                    updated_at=?,version=version+1 WHERE id=? AND version=?''',
                    (now, order_id, expected_version))
                if not changed.rowcount:
                    raise ValueError('Live-заявка уже изменилась; обновите страницу')
                self.store.db.execute('''UPDATE trade_plans SET status='cancelled',
                    reason_code='operator_cancelled_order',updated_at=? WHERE id=?''',
                    (now, order['plan_id']))
            return True
        if order['status'] not in ('open', 'partially_filled', 'unknown'):
            raise ValueError('Эту Live-заявку уже нельзя отменить')
        if not self.private or not self.private.allow_orders:
            raise ValueError('Приватные записи MEXC отключены')
        now = utc_now()
        with self.store.db:
            changed = self.store.db.execute('''UPDATE live_orders SET status='cancel_requested',
                updated_at=?,version=version+1 WHERE id=? AND version=?''',
                (now, order_id, expected_version))
            if not changed.rowcount:
                raise ValueError('Live-заявка уже изменилась; обновите страницу')
        try:
            await self.private.cancel_external(order['symbol'], order['external_oid'])
        except MexcPrivateError as error:
            with self.store.db:
                self.store.db.execute('''UPDATE live_orders SET error=?,updated_at=?
                    WHERE id=?''', (str(error)[:500], utc_now(), order_id))
            self._disarm('Live order cancellation result is uncertain')
        return True

    async def _reconcile_account(self):
        try:
            orders = await self.private.open_orders()
            known = {str(row[0]) for row in self.store.db.execute('''SELECT external_oid
                FROM live_orders WHERE status IN ('submitting','unknown','open',
                'partially_filled','cancel_requested')''')}
            foreign = [item for item in orders
                       if str(item.get('externalOid') or '') not in known]
            if foreign:
                raise LiveExecutionError('Unowned MEXC order appeared')
            await self.sync_positions()
            self._last_account_sync = time.monotonic()
            return True
        except (MexcPrivateError, LiveExecutionError) as error:
            if self.store.app_setting('live_armed', 'false') == 'true':
                self._disarm(str(error))
            return False

    async def step(self):
        mode = self.store.app_setting('execution_mode', 'recognition')
        if self.store.app_setting('live_armed', 'false') != 'true':
            self._require_reapproval('Live latch is not active')
        if not self.private:
            return False
        has_live_state = self.store.db.execute('''SELECT 1 FROM positions
            WHERE environment='live' AND status IN ('pending','open','closing') LIMIT 1''').fetchone()
        if (mode == 'live' or has_live_state or self._active_order()) and (
                time.monotonic() - self._last_account_sync >= self.account_sync_interval):
            await self._reconcile_account()
        order = self._active_order()
        if order:
            if order['status'] in ('unknown', 'open', 'partially_filled', 'submitting',
                                   'cancel_requested'):
                return await self._reconcile_order(order)
            if mode != 'live':
                return False
            if self.store.app_setting('live_armed', 'false') != 'true':
                return False
            if not self.private.allow_orders:
                self._disarm('Live API writes are not enabled in local configuration')
                return False
            if not await self._trigger_ready(order):
                return False
            return await self._submit(order)
        if mode != 'live':
            return False
        if self.store.app_setting('live_armed', 'false') != 'true':
            return False
        if not self.private.allow_orders:
            self._disarm('Live API writes are not enabled in local configuration')
            return False
        plan = self._plan()
        if plan:
            try:
                return await self._prepare(plan)
            except LiveExecutionError as error:
                now = utc_now()
                with self.store.db:
                    self.store.db.execute('''UPDATE trade_plans SET status='failed',
                        reason_code='live_preparation_failed',updated_at=? WHERE id=?''',
                        (now, plan['id']))
                self._disarm(str(error))
                return True
        return False
