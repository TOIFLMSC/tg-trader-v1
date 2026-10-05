"""Deterministic paper execution on public MEXC prices.

The executor never calls private endpoints.  It applies approved paper plans to
local orders, fills and positions, using bid/ask as a conservative fill model.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json

from trader.approvals import PlanReviewError
from trader.mexc_market import MarketDataError, MarketValidationError, validate_order_parameters
from trader.migrations import utc_now


class PaperExecutionError(ValueError):
    pass


def _decimal(value, label='value', allow_zero=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PaperExecutionError(f'{label}: некорректное число') from None
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise PaperExecutionError(f'{label}: некорректное число')
    return number


def _signed(value, label='value'):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PaperExecutionError(f'{label}: некорректное число') from None
    if not number.is_finite():
        raise PaperExecutionError(f'{label}: некорректное число')
    return number


def _text(value):
    value = Decimal(value)
    text = format(value.normalize(), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _json(value, fallback):
    try:
        return json.loads(value or '')
    except (TypeError, json.JSONDecodeError):
        return fallback


def _price(ticker, side, opening):
    if opening:
        raw = ticker.get('ask1') if side == 'long' else ticker.get('bid1')
    else:
        raw = ticker.get('bid1') if side == 'long' else ticker.get('ask1')
    return _decimal(raw or ticker.get('lastPrice'), 'цена исполнения')


def _gross_pnl(side, entry, exit_price, base_quantity):
    move = exit_price - entry if side == 'long' else entry - exit_price
    return move * base_quantity


def _timestamp(value, label):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        raise PaperExecutionError(f'{label}: некорректное время') from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PaperExecutor:
    def __init__(self, store, market, mark_interval_seconds=2, funding_interval_seconds=60,
                 recovery_gap_seconds=60, recovery_max_candles=2000):
        self.store = store
        self.market = market
        self.mark_interval_seconds = mark_interval_seconds
        self.funding_interval_seconds = funding_interval_seconds
        self.recovery_gap_seconds = recovery_gap_seconds
        self.recovery_max_candles = recovery_max_candles

    def _notify(self, db, key, text):
        db.execute('''INSERT OR IGNORE INTO paper_notifications(dedupe_key,text,created_at)
                      VALUES (?,?,?)''', (key, text[:3500], utc_now()))

    def _next_approved(self):
        return self.store.db.execute('''
            SELECT * FROM trade_plans
            WHERE environment='paper' AND status='approved'
            ORDER BY id LIMIT 1''').fetchone()

    def _next_order(self):
        return self.store.db.execute('''
            SELECT * FROM paper_orders WHERE status IN ('pending_trigger','open')
            ORDER BY id LIMIT 1''').fetchone()

    def _next_position(self):
        return self.store.db.execute('''
            SELECT * FROM positions WHERE environment='paper' AND status='open'
              AND (last_mark_at IS NULL
                   OR (julianday('now')-julianday(last_mark_at))*86400 >= ?)
            ORDER BY CASE WHEN last_mark_at IS NULL THEN 0 ELSE 1 END,last_mark_at,id LIMIT 1
            ''', (self.mark_interval_seconds,)).fetchone()

    def auto_approve_next(self):
        if self.store.app_setting('approval_mode', 'manual') != 'auto':
            return False
        row = self.store.db.execute('''
            SELECT p.id,p.version FROM trade_plans p
            JOIN market_checks mc ON mc.id=p.market_check_id
            WHERE p.environment='paper' AND p.status='ready' AND mc.status='valid'
              AND (julianday('now')-julianday(mc.checked_at))*86400 <= 300
            ORDER BY p.id LIMIT 1''').fetchone()
        if not row:
            return False
        try:
            self.store.review_plan(
                row['id'], row['version'], 'approve', {}, None, 'paper-auto',
                f'paper-auto-plan-{row["id"]}-v{row["version"]}')
        except PlanReviewError:
            return False
        with self.store.db:
            self._notify(
                self.store.db, f'paper-auto-approved:{row["id"]}',
                f'PAPER · План #{row["id"]} автоматически подтверждён после проверки MEXC.')
        return True

    async def step(self):
        if self.store.app_setting('execution_mode', 'recognition') != 'paper':
            return False
        if self.auto_approve_next():
            return True
        plan = self._next_approved()
        if plan:
            await self.execute_plan(plan)
            return True
        order = self._next_order()
        if order:
            await self.advance_order(order)
            return True
        position = self._next_position()
        if position:
            return await self.mark_position(position)
        return False

    async def _market(self, symbol):
        mexc_symbol = symbol[:-4] + '_USDT'
        contract = await self.market.contract(mexc_symbol)
        if not contract:
            raise PaperExecutionError(f'Контракт {mexc_symbol} не найден')
        ticker = await self.market.ticker(mexc_symbol)
        return contract, ticker

    def _fail_plan(self, plan_id, reason):
        now = utc_now()
        with self.store.db:
            changed = self.store.db.execute('''UPDATE trade_plans
                SET status='failed',reason_code='paper_execution_failed',updated_at=?
                WHERE id=? AND status='approved' ''', (now, plan_id))
            if changed.rowcount:
                self.store.db.execute('''INSERT OR IGNORE INTO paper_orders(
                    plan_id,client_key,symbol,side,intent,order_kind,status,error,created_at,updated_at)
                    SELECT id,? ,COALESCE(symbol,'UNKNOWN'),COALESCE(side,'long'),action,
                           CASE WHEN order_kind='trigger_limit' THEN 'trigger_limit' ELSE 'market' END,
                           'failed',?,?,? FROM trade_plans WHERE id=?''',
                    (f'paper:plan:{plan_id}', reason[:500], now, now, plan_id))
                self._notify(
                    self.store.db, f'paper-plan-failed:{plan_id}',
                    f'PAPER · План #{plan_id} не исполнен: {reason[:500]}')

    async def execute_plan(self, plan):
        try:
            if plan['action'] in ('set_stop', 'set_take_profit'):
                self._apply_protection(plan)
                return
            if plan['action'] in ('open', 'add'):
                contract, ticker = await self._market(plan['symbol'])
                if plan['order_kind'] == 'trigger_limit':
                    self._create_trigger_order(plan, contract, ticker)
                else:
                    estimate = validate_order_parameters(contract, ticker, dict(plan))
                    self._fill_entry(plan, contract, ticker, estimate)
                return
            if plan['action'] in ('close_partial', 'close_full'):
                position = self.store.db.execute(
                    'SELECT * FROM positions WHERE id=? AND status=\'open\'',
                    (plan['target_position_id'],)).fetchone()
                if not position:
                    raise PaperExecutionError('Открытая paper-позиция не найдена')
                contract, ticker = await self._market(position['symbol'])
                await self._apply_funding(
                    position, _decimal(ticker.get('lastPrice'), 'mark'), force=True)
                position = self.store.db.execute(
                    'SELECT * FROM positions WHERE id=? AND status=\'open\'',
                    (plan['target_position_id'],)).fetchone()
                if not position:
                    raise PaperExecutionError('Paper-позиция закрылась до исполнения плана')
                self._fill_close(plan, position, contract, ticker)
                return
            raise PaperExecutionError('Действие не поддержано paper executor')
        except (PaperExecutionError, MarketDataError, MarketValidationError) as error:
            self._fail_plan(plan['id'], str(error))

    def _order_row(self, db, plan, status, contracts=None, position_id=None):
        now = utc_now()
        order_kind = plan['order_kind'] if plan['order_kind'] in ('market', 'trigger_limit') else 'market'
        cursor = db.execute('''INSERT INTO paper_orders(
            plan_id,position_id,client_key,symbol,side,intent,order_kind,status,
            trigger_price,limit_price,requested_contracts,reduce_only,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            plan['id'], position_id, f'paper:plan:{plan["id"]}', plan['symbol'], plan['side'],
            plan['action'], order_kind, status, plan['trigger_price'], plan['limit_price'],
            _text(contracts) if contracts is not None else None,
            1 if plan['action'].startswith('close_') else 0, now, now))
        return cursor.lastrowid

    def _create_trigger_order(self, plan, contract, ticker):
        level = _decimal(plan['limit_price'], 'limit price')
        synthetic = dict(ticker)
        synthetic['ask1' if plan['side'] == 'long' else 'bid1'] = _text(level)
        estimate = validate_order_parameters(contract, synthetic, dict(plan))
        contracts = _decimal(estimate['estimated_contracts'], 'contracts')
        now = utc_now()
        last = _decimal(ticker.get('lastPrice'), 'last price')
        triggered = last >= level if plan['side'] == 'long' else last <= level
        status = 'open' if triggered else 'pending_trigger'
        with self.store.db:
            order_id = self._order_row(self.store.db, plan, status, contracts)
            self.store.db.execute(
                "UPDATE trade_plans SET status='executing',updated_at=? WHERE id=? AND status='approved'",
                (now, plan['id']))
            if triggered:
                self.store.db.execute(
                    'UPDATE paper_orders SET triggered_at=? WHERE id=?', (now, order_id))
            self._notify(
                self.store.db, f'paper-order-created:{order_id}',
                f'PAPER · План #{plan["id"]}: trigger-limit {plan["symbol"]} '
                f'{plan["side"].upper()} создан на уровне {_text(level)}; '
                f'{_text(contracts)} контрактов.')

    def _fee_rate(self, contract):
        # A missing fee would make paper performance systematically optimistic.
        # Fail the plan instead of silently treating an incomplete market
        # specification as zero commission.
        return _decimal(contract.get('takerFeeRate'), 'taker fee', allow_zero=True)

    def _fill_entry(self, plan, contract, ticker, estimate, order=None):
        contracts = _decimal(
            order['requested_contracts'] if order else estimate['estimated_contracts'], 'contracts')
        contract_size = _decimal(contract.get('contractSize'), 'contract size')
        fill_price = (_decimal(order['limit_price'], 'limit price') if order
                      else _price(ticker, plan['side'], True))
        base_quantity = contracts * contract_size
        notional = base_quantity * fill_price
        fee = notional * self._fee_rate(contract)
        margin = _decimal(plan['margin_usdt'], 'margin')
        now = utc_now()
        with self.store.db:
            if order:
                order_id = order['id']
            else:
                order_id = self._order_row(self.store.db, plan, 'filled', contracts)
            if plan['action'] == 'open':
                cursor = self.store.db.execute('''INSERT INTO positions(
                    environment,channel_id,opening_plan_id,symbol,side,leverage,isolated,status,
                    initial_margin_usdt,allocated_margin_usdt,quantity,remaining_quantity,
                    average_entry_price,stop_price,take_profits_json,contract_size,
                    realized_pnl_usdt,unrealized_pnl_usdt,entry_fees_usdt,exit_fees_usdt,
                    mark_price,opened_at,created_at,updated_at)
                    VALUES ('paper',?,?,?,?,?,1,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                    plan['channel_id'], plan['id'], plan['symbol'], plan['side'],
                    plan['effective_leverage'], _text(margin), _text(margin), _text(contracts),
                    _text(contracts), _text(fill_price), plan['stop_price'],
                    plan['take_profits_json'], _text(contract_size), _text(-fee), '0',
                    _text(fee), '0', _text(fill_price), now, now, now))
                position_id = cursor.lastrowid
            else:
                position = self.store.db.execute('''SELECT * FROM positions
                    WHERE id=? AND environment='paper' AND status='open' ''',
                    (plan['target_position_id'],)).fetchone()
                if not position or position['channel_id'] != plan['channel_id']:
                    raise PaperExecutionError('Позиция для добавления не найдена')
                if position['side'] != plan['side']:
                    raise PaperExecutionError('Направление добавления не совпадает с позицией')
                old_contracts = _decimal(position['remaining_quantity'], 'remaining')
                old_base = old_contracts * _decimal(position['contract_size'], 'contract size')
                average = ((_decimal(position['average_entry_price'], 'entry') * old_base
                            + fill_price * base_quantity) / (old_base + base_quantity))
                position_id = position['id']
                self.store.db.execute('''UPDATE positions SET
                    initial_margin_usdt=?,allocated_margin_usdt=?,quantity=?,remaining_quantity=?,
                    average_entry_price=?,realized_pnl_usdt=?,entry_fees_usdt=?,mark_price=?,
                    updated_at=?,version=version+1 WHERE id=?''', (
                    _text(_decimal(position['initial_margin_usdt']) + margin),
                    _text(_decimal(position['allocated_margin_usdt']) + margin),
                    _text(_decimal(position['quantity']) + contracts),
                    _text(old_contracts + contracts), _text(average),
                    _text(_signed(position['realized_pnl_usdt'], 'realized PnL') - fee),
                    _text(_decimal(position['entry_fees_usdt'], allow_zero=True) + fee),
                    _text(fill_price), now, position_id))
            self.store.db.execute('''UPDATE paper_orders SET position_id=?,status='filled',
                filled_contracts=?,average_fill_price=?,filled_at=?,updated_at=? WHERE id=?''',
                (position_id, _text(contracts), _text(fill_price), now, now, order_id))
            self.store.db.execute('''INSERT INTO paper_fills(
                order_id,position_id,price,contracts,base_quantity,quote_notional_usdt,
                fee_usdt,created_at) VALUES (?,?,?,?,?,?,?,?)''', (
                order_id, position_id, _text(fill_price), _text(contracts),
                _text(base_quantity), _text(notional), _text(fee), now))
            self.store.db.execute(
                "UPDATE trade_plans SET status='executed',reason_code=NULL,updated_at=? WHERE id=?",
                (now, plan['id']))
            self._notify(
                self.store.db, f'paper-entry-filled:{order_id}',
                f'PAPER · {plan["symbol"]} {plan["side"].upper()} исполнен по '
                f'{_text(fill_price)}: {_text(contracts)} контрактов, маржа {_text(margin)} USDT.')

    def _fill_close(self, plan, position, contract, ticker):
        remaining = _decimal(position['remaining_quantity'], 'remaining')
        if plan['action'] == 'close_full':
            contracts = remaining
        else:
            percent = _decimal(plan['close_percent'], 'close percent')
            unit = _decimal(contract.get('volUnit'), 'volume unit')
            contracts = ((remaining * percent / Decimal(100)) / unit).to_integral_value(
                rounding=ROUND_FLOOR) * unit
            if contracts <= 0:
                raise PaperExecutionError('Доля закрытия меньше минимального шага контракта')
        self._close_position(position, contract, ticker, contracts, plan=plan)

    def _close_position(self, position, contract, ticker, contracts, plan=None, intent=None,
                        price_source='live_bid_ask', source_candle_time=None):
        remaining = _decimal(position['remaining_quantity'], 'remaining')
        contracts = min(_decimal(contracts, 'contracts'), remaining)
        contract_size = _decimal(position['contract_size'], 'contract size')
        fill_price = _price(ticker, position['side'], False)
        base_quantity = contracts * contract_size
        notional = base_quantity * fill_price
        fee = notional * self._fee_rate(contract)
        gross = _gross_pnl(
            position['side'], _decimal(position['average_entry_price'], 'entry'),
            fill_price, base_quantity)
        net_fill = gross - fee
        new_remaining = remaining - contracts
        old_allocated = _decimal(position['allocated_margin_usdt'], 'allocated margin')
        new_allocated = old_allocated * new_remaining / remaining
        now = utc_now()
        action = plan['action'] if plan else intent
        key = f'paper:plan:{plan["id"]}' if plan else f'paper:{intent}:position:{position["id"]}'
        final_status = 'liquidated' if intent == 'liquidation' else ('closed' if new_remaining == 0 else 'open')
        with self.store.db:
            cursor = self.store.db.execute('''INSERT INTO paper_orders(
                plan_id,position_id,client_key,symbol,side,intent,order_kind,status,
                requested_contracts,filled_contracts,average_fill_price,reduce_only,
                price_source,source_candle_time,created_at,filled_at,updated_at)
                VALUES (?,?,?,?,?,?,'market','filled',?,?,?,1,?,?,?,?,?)''', (
                plan['id'] if plan else None, position['id'], key, position['symbol'],
                position['side'], action, _text(contracts), _text(contracts), _text(fill_price),
                price_source, source_candle_time, now, now, now))
            order_id = cursor.lastrowid
            self.store.db.execute('''UPDATE positions SET remaining_quantity=?,
                allocated_margin_usdt=?,status=?,realized_pnl_usdt=?,unrealized_pnl_usdt='0',
                exit_fees_usdt=?,mark_price=?,closed_at=?,updated_at=?,version=version+1
                WHERE id=?''', (
                _text(new_remaining), _text(new_allocated), final_status,
                _text(_signed(position['realized_pnl_usdt'], 'realized PnL') + net_fill),
                _text(_decimal(position['exit_fees_usdt'], allow_zero=True) + fee),
                _text(fill_price), now if new_remaining == 0 else None, now, position['id']))
            self.store.db.execute('''INSERT INTO paper_fills(
                order_id,position_id,price,contracts,base_quantity,quote_notional_usdt,
                fee_usdt,realized_pnl_usdt,price_source,source_candle_time,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (
                order_id, position['id'], _text(fill_price), _text(contracts),
                _text(base_quantity), _text(notional), _text(fee), _text(net_fill),
                price_source, source_candle_time, now))
            if plan:
                self.store.db.execute(
                    "UPDATE trade_plans SET status='executed',reason_code=NULL,updated_at=? WHERE id=?",
                    (now, plan['id']))
            self._notify(
                self.store.db, f'paper-close-filled:{order_id}',
                f'PAPER · {position["symbol"]} закрыто {_text(contracts)} контрактов по '
                f'{_text(fill_price)}; PnL исполнения {_text(net_fill)} USDT. '
                f'Остаток: {_text(new_remaining)}.')
            if price_source == 'recovery_level':
                self.store.db.execute('''INSERT INTO audit_log(
                    actor,action,entity_type,entity_id,after_json,created_at)
                    VALUES ('paper-executor','paper_position.recovered_exit','position',?,?,?)''', (
                    str(position['id']), json.dumps({
                        'intent': action, 'price': _text(fill_price),
                        'source_candle_time': source_candle_time,
                    }, ensure_ascii=False, sort_keys=True), now))

    def _apply_protection(self, plan):
        position = self.store.db.execute(
            "SELECT * FROM positions WHERE id=? AND environment='paper' AND status='open'",
            (plan['target_position_id'],)).fetchone()
        if not position or position['channel_id'] != plan['channel_id']:
            raise PaperExecutionError('Открытая paper-позиция не найдена')
        now = utc_now()
        with self.store.db:
            order_id = self._order_row(self.store.db, plan, 'filled', Decimal(0), position['id'])
            if plan['action'] == 'set_stop':
                self.store.db.execute(
                    'UPDATE positions SET stop_price=?,updated_at=?,version=version+1 WHERE id=?',
                    (plan['stop_price'], now, position['id']))
                detail = f'стоп {plan["stop_price"]}'
            else:
                self.store.db.execute(
                    'UPDATE positions SET take_profits_json=?,updated_at=?,version=version+1 WHERE id=?',
                    (plan['take_profits_json'], now, position['id']))
                detail = 'тейки ' + ', '.join(_json(plan['take_profits_json'], []))
            self.store.db.execute('''UPDATE paper_orders SET position_id=?,filled_at=?,updated_at=?
                                     WHERE id=?''', (position['id'], now, now, order_id))
            self.store.db.execute(
                "UPDATE trade_plans SET status='executed',reason_code=NULL,updated_at=? WHERE id=?",
                (now, plan['id']))
            self._notify(
                self.store.db, f'paper-protection:{order_id}',
                f'PAPER · {position["symbol"]}: установлен {detail}.')

    async def advance_order(self, order):
        plan = self.store.db.execute('SELECT * FROM trade_plans WHERE id=?',
                                     (order['plan_id'],)).fetchone()
        if not plan or plan['status'] != 'executing':
            return
        try:
            contract, ticker = await self._market(order['symbol'])
            now = utc_now()
            if order['status'] == 'pending_trigger':
                last = _decimal(ticker.get('lastPrice'), 'last price')
                trigger = _decimal(order['trigger_price'], 'trigger')
                hit = last >= trigger if order['side'] == 'long' else last <= trigger
                if not hit:
                    return
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE paper_orders SET status='open',triggered_at=?,updated_at=? WHERE id=?",
                        (now, now, order['id']))
                order = self.store.db.execute(
                    'SELECT * FROM paper_orders WHERE id=?', (order['id'],)).fetchone()
            limit_price = _decimal(order['limit_price'], 'limit price')
            fillable = (_decimal(ticker.get('ask1') or ticker.get('lastPrice')) <= limit_price
                        if order['side'] == 'long' else
                        _decimal(ticker.get('bid1') or ticker.get('lastPrice')) >= limit_price)
            if fillable:
                self._fill_entry(plan, contract, ticker, {}, order=order)
        except (PaperExecutionError, MarketDataError, MarketValidationError) as error:
            now = utc_now()
            with self.store.db:
                self.store.db.execute(
                    "UPDATE paper_orders SET status='failed',error=?,updated_at=? WHERE id=?",
                    (str(error)[:500], now, order['id']))
                self.store.db.execute(
                    "UPDATE trade_plans SET status='failed',reason_code='paper_execution_failed',updated_at=? WHERE id=?",
                    (now, plan['id']))
                self._notify(
                    self.store.db, f'paper-order-failed:{order["id"]}',
                    f'PAPER · Ордер #{order["id"]} не исполнен: {str(error)[:500]}')

    def _funding_due(self, position, now):
        raw = position['last_funding_check_at']
        if not raw:
            return True
        try:
            checked = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return (now - checked.astimezone(timezone.utc)).total_seconds() >= self.funding_interval_seconds

    async def _apply_funding(self, position, mark, force=False):
        now_dt = datetime.now(timezone.utc)
        if not force and not self._funding_due(position, now_dt):
            return False
        now = now_dt.isoformat()
        try:
            history = await self.market.funding_history(position['symbol'][:-4] + '_USDT')
        except MarketDataError:
            with self.store.db:
                self.store.db.execute(
                    'UPDATE positions SET last_funding_check_at=? WHERE id=? AND status=\'open\'',
                    (now, position['id']))
            return False
        lower_raw = position['last_funding_at'] or position['opened_at'] or position['created_at']
        try:
            lower = datetime.fromisoformat(lower_raw.replace('Z', '+00:00'))
            if lower.tzinfo is None:
                lower = lower.replace(tzinfo=timezone.utc)
            lower = lower.astimezone(timezone.utc)
        except (AttributeError, ValueError):
            raise PaperExecutionError('Некорректное время открытия paper-позиции') from None
        remaining = _decimal(position['remaining_quantity'], 'remaining')
        contract_size = _decimal(position['contract_size'], 'contract size')
        position_value = remaining * contract_size * mark
        total = Decimal(0)
        latest = position['last_funding_at']
        inserted = False
        with self.store.db:
            for record in history:
                try:
                    settled = datetime.fromisoformat(record['settle_time'].replace('Z', '+00:00'))
                    if settled.tzinfo is None:
                        settled = settled.replace(tzinfo=timezone.utc)
                    settled = settled.astimezone(timezone.utc)
                except (KeyError, AttributeError, ValueError):
                    raise PaperExecutionError('Некорректное время funding settlement') from None
                if settled <= lower or settled > now_dt:
                    continue
                rate = _signed(record.get('rate'), 'funding rate')
                amount = position_value * rate * (Decimal(-1) if position['side'] == 'long'
                                                  else Decimal(1))
                cursor = self.store.db.execute('''INSERT OR IGNORE INTO paper_funding(
                    position_id,symbol,side,rate,position_value_usdt,amount_usdt,
                    settle_time,price_source,created_at) VALUES (?,?,?,?,?,?,?,?,?)''', (
                    position['id'], position['symbol'], position['side'], _text(rate),
                    _text(position_value), _text(amount), settled.isoformat(),
                    'last_price_at_processing', now))
                if cursor.rowcount:
                    inserted = True
                    total += amount
                    latest = settled.isoformat()
                    self._notify(
                        self.store.db,
                        f'paper-funding:{position["id"]}:{int(settled.timestamp())}',
                        f'PAPER · {position["symbol"]}: funding {_text(amount)} USDT '
                        f'по ставке {_text(rate)}.')
            if inserted:
                self.store.db.execute('''UPDATE positions SET
                    funding_pnl_usdt=?,realized_pnl_usdt=?,last_funding_at=?,
                    last_funding_check_at=?,updated_at=?,version=version+1
                    WHERE id=? AND status='open' ''', (
                    _text(_signed(position['funding_pnl_usdt'], 'funding PnL') + total),
                    _text(_signed(position['realized_pnl_usdt'], 'realized PnL') + total),
                    latest, now, now, position['id']))
            else:
                self.store.db.execute(
                    'UPDATE positions SET last_funding_check_at=? WHERE id=? AND status=\'open\'',
                    (now, position['id']))
        return inserted

    def _liquidation_price(self, position, contract):
        """Return the executor's approximate isolated liquidation threshold."""
        remaining = _decimal(position['remaining_quantity'], 'remaining')
        base = remaining * _decimal(position['contract_size'], 'contract size')
        entry = _decimal(position['average_entry_price'], 'entry')
        margin = _decimal(position['allocated_margin_usdt'], 'margin')
        funding = _signed(position['funding_pnl_usdt'], 'funding PnL')
        maintenance = _decimal(
            contract.get('maintenanceMarginRate'), 'maintenance rate', allow_zero=True)
        if position['side'] == 'long':
            denominator = base * (Decimal(1) - maintenance)
            numerator = entry * base - margin - funding
        else:
            denominator = base * (Decimal(1) + maintenance)
            numerator = margin + funding + entry * base
        if denominator <= 0:
            raise PaperExecutionError('Некорректная ставка поддерживающей маржи')
        threshold = numerator / denominator
        return threshold if threshold.is_finite() and threshold > 0 else None

    def _recovery_candidates(self, position, contract, high, low):
        candidates = []
        stop = (_decimal(position['stop_price'], 'stop')
                if position['stop_price'] not in (None, '') else None)
        if stop and ((position['side'] == 'long' and low <= stop)
                     or (position['side'] == 'short' and high >= stop)):
            candidates.append({'intent': 'stop_loss', 'price': _text(stop)})

        targets = [_decimal(value, 'take profit')
                   for value in _json(position['take_profits_json'], [])]
        if len(targets) == 1:
            target = targets[0]
            if ((position['side'] == 'long' and high >= target)
                    or (position['side'] == 'short' and low <= target)):
                candidates.append({'intent': 'take_profit', 'price': _text(target)})

        liquidation = self._liquidation_price(position, contract)
        if liquidation and ((position['side'] == 'long' and low <= liquidation)
                            or (position['side'] == 'short' and high >= liquidation)):
            candidates.append({'intent': 'liquidation', 'price': _text(liquidation)})
        return candidates

    def _set_recovery_attention(self, position, reason, candle=None, candidates=None):
        candle = candle or {}
        candidates = candidates or []
        now = utc_now()
        candle_time = (datetime.fromtimestamp(int(candle['time']), tz=timezone.utc).isoformat()
                       if candle.get('time') is not None else None)
        high = _text(_decimal(candle['high'], 'candle high')) if candle.get('high') else None
        low = _text(_decimal(candle['low'], 'candle low')) if candle.get('low') else None
        evidence = {
            'reason': reason, 'candle_time': candle_time, 'high': high, 'low': low,
            'candidates': candidates,
        }
        with self.store.db:
            changed = self.store.db.execute('''UPDATE positions SET
                recovery_status='attention',recovery_reason=?,recovery_candle_time=?,
                recovery_high=?,recovery_low=?,recovery_options_json=?,last_recovery_at=?,
                updated_at=?,version=version+1
                WHERE id=? AND status='open' AND recovery_status='ok' ''', (
                reason, candle_time, high, low,
                json.dumps(candidates, ensure_ascii=False, sort_keys=True), now, now,
                position['id']))
            if not changed.rowcount:
                return False
            self.store.db.execute('''INSERT INTO audit_log(
                actor,action,entity_type,entity_id,after_json,created_at)
                VALUES ('paper-executor','paper_position.recovery_attention','position',?,?,?)''', (
                str(position['id']), json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True), now))
            detail = (f'свеча {low}–{high}' if high and low else
                      f'разрыв больше {self.recovery_max_candles} минут')
            self._notify(
                self.store.db,
                f'paper-recovery-attention:{position["id"]}:{candle_time or reason}',
                f'PAPER · {position["symbol"]}: восстановление требует решения владельца; '
                f'{detail}. Автоматические защитные выходы приостановлены.')
        return True

    async def _recover_position(self, position, contract):
        lower_raw = position['last_mark_at'] or position['opened_at'] or position['created_at']
        lower = _timestamp(lower_raw, 'время последней paper-котировки')
        now = datetime.now(timezone.utc)
        gap_seconds = max(0, (now - lower).total_seconds())
        if gap_seconds < self.recovery_gap_seconds:
            return None
        if gap_seconds > self.recovery_max_candles * 60:
            self._set_recovery_attention(position, 'history_window_exceeded')
            return 'attention'

        rows = await self.market.candle_rows(
            position['symbol'][:-4] + '_USDT', int(lower.timestamp()),
            int(now.timestamp()), 'Min1')
        for candle in rows:
            high = _decimal(candle.get('high'), 'candle high')
            low = _decimal(candle.get('low'), 'candle low')
            if low > high:
                raise PaperExecutionError('MEXC вернул свечу с low выше high')
            candidates = self._recovery_candidates(position, contract, high, low)
            if not candidates:
                continue
            candle_time = datetime.fromtimestamp(int(candle['time']), tz=timezone.utc)
            overlap = candle_time < lower
            if overlap or len(candidates) > 1:
                reason = 'overlapping_candle' if overlap else 'ambiguous_protection_order'
                self._set_recovery_attention(position, reason, candle, candidates)
                return 'attention'

            candidate = candidates[0]
            price = candidate['price']
            recovered_ticker = {
                'lastPrice': price,
                'bid1': price if position['side'] == 'long' else None,
                'ask1': price if position['side'] == 'short' else None,
            }
            self._close_position(
                position, contract, recovered_ticker,
                _decimal(position['remaining_quantity'], 'remaining'),
                intent=candidate['intent'], price_source='recovery_level',
                source_candle_time=candle_time.isoformat())
            return 'closed'
        return None

    async def mark_position(self, position):
        try:
            contract, ticker = await self._market(position['symbol'])
            mark = _decimal(ticker.get('lastPrice'), 'mark')
            await self._apply_funding(position, mark)
            position = self.store.db.execute(
                'SELECT * FROM positions WHERE id=?', (position['id'],)).fetchone()
            remaining = _decimal(position['remaining_quantity'], 'remaining')
            base = remaining * _decimal(position['contract_size'], 'contract size')
            unrealized = _gross_pnl(
                position['side'], _decimal(position['average_entry_price'], 'entry'), mark, base)
            if position['recovery_status'] == 'attention':
                now = utc_now()
                with self.store.db:
                    self.store.db.execute('''UPDATE positions SET mark_price=?,
                        unrealized_pnl_usdt=?,last_mark_at=?,updated_at=?
                        WHERE id=? AND status='open' ''',
                        (_text(mark), _text(unrealized), now, now, position['id']))
                return True

            recovery = await self._recover_position(position, contract)
            if recovery == 'closed':
                return True
            if recovery == 'attention':
                now = utc_now()
                with self.store.db:
                    self.store.db.execute('''UPDATE positions SET mark_price=?,
                        unrealized_pnl_usdt=?,last_mark_at=?,updated_at=?
                        WHERE id=? AND status='open' ''',
                        (_text(mark), _text(unrealized), now, now, position['id']))
                return True

            now = utc_now()
            with self.store.db:
                self.store.db.execute('''UPDATE positions SET mark_price=?,unrealized_pnl_usdt=?,
                    last_mark_at=?,updated_at=?,version=version+1 WHERE id=? AND status='open' ''',
                    (_text(mark), _text(unrealized), now, now, position['id']))
            position = self.store.db.execute(
                'SELECT * FROM positions WHERE id=?', (position['id'],)).fetchone()
            stop = (_decimal(position['stop_price'], 'stop')
                    if position['stop_price'] not in (None, '') else None)
            stop_hit = bool(stop and ((position['side'] == 'long' and mark <= stop)
                                     or (position['side'] == 'short' and mark >= stop)))
            maintenance = (base * mark
                           * _decimal(contract.get('maintenanceMarginRate'),
                                      'maintenance rate', allow_zero=True))
            liquidated = (_decimal(position['allocated_margin_usdt'], 'margin') + unrealized
                          + _signed(position['funding_pnl_usdt'], 'funding PnL')
                          <= maintenance)
            if stop_hit:
                self._close_position(position, contract, ticker, remaining, intent='stop_loss')
                return True
            if liquidated:
                self._close_position(position, contract, ticker, remaining, intent='liquidation')
                return True
            targets = [_decimal(value, 'take profit')
                       for value in _json(position['take_profits_json'], [])]
            hit = [target for target in targets
                   if ((position['side'] == 'long' and mark >= target)
                       or (position['side'] == 'short' and mark <= target))]
            if len(targets) == 1 and hit:
                self._close_position(position, contract, ticker, remaining, intent='take_profit')
            elif len(targets) > 1 and hit:
                with self.store.db:
                    self._notify(
                        self.store.db,
                        f'paper-tp-allocation:{position["id"]}:{_text(hit[-1])}',
                        f'PAPER · {position["symbol"]}: достигнут тейк {_text(hit[-1])}, '
                        'но для нескольких целей не заданы доли закрытия. Позиция не изменена.')
            return True
        except (PaperExecutionError, MarketDataError):
            # Signal the worker to use its idle backoff.  A malformed or
            # unavailable public quote must not create a tight retry loop.
            return False
