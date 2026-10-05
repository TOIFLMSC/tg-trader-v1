"""Version-bound manual review of trade plans.

Approving a plan never executes it.  This module only validates operator
adjustments, records an immutable decision, and transitions the local plan.
"""
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import json

from trader.migrations import utc_now
from trader.mexc_market import (APPROVABLE_STATUSES, MarketDataError,
                                MarketValidationError, validate_order_parameters)
from trader.planner import ACTIVE_POSITION_STATUSES, RESERVING_PLAN_STATUSES


class PlanReviewError(ValueError):
    pass


REVIEWABLE = ('preview', 'ready', 'needs_input', 'blocked')


def _decimal(value, label, maximum=None):
    if value in (None, ''):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise PlanReviewError(f'{label}: укажите положительное число') from None
    if not number.is_finite() or number <= 0:
        raise PlanReviewError(f'{label}: укажите положительное число')
    if maximum is not None and number > maximum:
        raise PlanReviewError(f'{label}: значение не должно превышать {maximum}')
    return number


def _number(number):
    if number is None:
        return None
    text = format(number.normalize(), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _snapshot(plan):
    fields = (
        'id', 'version', 'status', 'reason_code', 'effective_leverage', 'margin_usdt',
        'close_percent', 'questions_json', 'target_position_id',
        'bank_limit_snapshot_usdt', 'updated_at',
    )
    return {field: plan[field] for field in fields}


def _reserved_margin(db, plan):
    if plan['environment'] not in ('paper', 'live'):
        return Decimal(0)
    rows = db.execute('''
        SELECT margin_usdt FROM trade_plans
        WHERE channel_id=? AND environment=? AND id!=? AND status IN (?,?,?)''',
        (plan['channel_id'], plan['environment'], plan['id'], *RESERVING_PLAN_STATUSES)
    ).fetchall()
    positions = db.execute('''
        SELECT COALESCE(allocated_margin_usdt,initial_margin_usdt) AS reserved_margin
        FROM positions
        WHERE channel_id=? AND environment=? AND status IN (?,?,?)''',
        (plan['channel_id'], plan['environment'], *ACTIVE_POSITION_STATUSES)).fetchall()
    return (sum((Decimal(row[0]) for row in rows if row[0]), Decimal(0))
            + sum((Decimal(row[0]) for row in positions), Decimal(0)))


def _active_position(db, plan):
    environment = plan['environment'] if plan['environment'] in ('paper', 'live') else None
    params = [plan['symbol'], *ACTIVE_POSITION_STATUSES]
    clause = ''
    if environment:
        clause = ' AND environment=?'
        params.append(environment)
    return db.execute(f'''
        SELECT * FROM positions WHERE symbol=? AND status IN (?,?,?){clause}
        ORDER BY id LIMIT 1''', params).fetchone()


def _validate_market(plan, values, comment):
    if plan['action'] not in ('open', 'add'):
        return
    if not plan['market_check_id']:
        raise PlanReviewError('Публичная проверка MEXC ещё не завершена')
    status = plan['market_status']
    if status not in APPROVABLE_STATUSES:
        raise PlanReviewError('Проверка MEXC запрещает вход: ' + (plan['market_reason'] or status))
    try:
        checked = datetime.fromisoformat(plan['market_checked_at'].replace('Z', '+00:00'))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds() > 300:
            raise PlanReviewError('Проверка MEXC устарела; дождитесь фонового обновления')
    except (AttributeError, ValueError):
        raise PlanReviewError('Время проверки MEXC повреждено') from None
    if status == 'stale_review' and not comment:
        raise PlanReviewError(
            'Сигнал старше 10 минут; добавьте комментарий, подтверждающий актуальность входа')
    try:
        contract = json.loads(plan['market_contract_json'] or '{}')
        ticker = json.loads(plan['market_ticker_json'] or '{}')
        validate_order_parameters(contract, ticker, {
            'action': plan['action'], 'side': plan['side'],
            'order_kind': plan['order_kind'], 'trigger_price': plan['trigger_price'],
            'limit_price': plan['limit_price'],
            'effective_leverage': values['effective_leverage'],
            'margin_usdt': values['margin_usdt'],
        })
    except (json.JSONDecodeError, MarketDataError):
        raise PlanReviewError('Сохранённые данные MEXC повреждены; дождитесь новой проверки') from None
    except MarketValidationError as error:
        raise PlanReviewError('Проверка MEXC запрещает вход: ' + error.reason) from None


def _validate_ready(db, plan, values, comment=None):
    action = plan['action']
    questions = json.loads(values['questions_json'] or '[]')
    if questions:
        raise PlanReviewError('Ответьте на вопросы к плану перед подтверждением')
    if plan['environment'] == 'live':
        armed = db.execute(
            "SELECT value FROM app_settings WHERE key='live_armed'").fetchone()
        if not armed or armed[0] != 'true':
            raise PlanReviewError(
                'Live-защёлка снята. Сначала выполните свежую проверку MEXC и активируйте её')
    if action in ('open', 'add'):
        if not plan['symbol'] or not plan['side']:
            raise PlanReviewError('Тикер или направление сделки не определены')
        if plan['order_kind'] == 'unspecified':
            raise PlanReviewError('Тип входа не определён')
        if plan['order_kind'] == 'trigger_limit' and (
                not plan['trigger_price'] or not plan['limit_price']):
            raise PlanReviewError('Для trigger-limit нужен один подтверждённый уровень')
        if values['effective_leverage'] is None:
            raise PlanReviewError('Укажите плечо; предлагаемое значение — 1×')
        bank = _decimal(values['bank_limit_snapshot_usdt'], 'Банк канала')
        margin = _decimal(values['margin_usdt'], 'Маржа')
        if bank is None:
            raise PlanReviewError('Сначала настройте банк канала')
        if margin is None:
            raise PlanReviewError('Укажите маржу')
        if margin + _reserved_margin(db, plan) > bank:
            raise PlanReviewError('Маржа превышает свободный лимит банка канала')
        _validate_market(plan, values, comment)
        if plan['environment'] in ('paper', 'live'):
            position = _active_position(db, plan)
            if action == 'open' and position:
                raise PlanReviewError('По монете уже есть активная позиция')
            reserved = db.execute('''
                SELECT 1 FROM trade_plans WHERE id!=? AND symbol=? AND environment=?
                AND status IN (?,?,?) LIMIT 1''',
                (plan['id'], plan['symbol'], plan['environment'], *RESERVING_PLAN_STATUSES)
            ).fetchone()
            if action == 'open' and reserved:
                raise PlanReviewError('Монета уже зарезервирована более ранним планом')
        if action == 'add':
            position = _active_position(db, plan)
            if not position or position['channel_id'] != plan['channel_id']:
                raise PlanReviewError('Нет собственной открытой позиции для добавления')
            values['target_position_id'] = position['id']
    elif action in ('close_partial', 'close_full', 'set_stop', 'set_take_profit'):
        position = _active_position(db, plan)
        if not position:
            raise PlanReviewError('Нет открытой позиции по указанному тикеру')
        if position['channel_id'] != plan['channel_id']:
            raise PlanReviewError('Позиция принадлежит другому каналу')
        values['target_position_id'] = position['id']
        if action == 'close_partial' and _decimal(values['close_percent'], 'Доля закрытия', Decimal(100)) is None:
            raise PlanReviewError('Укажите долю частичного закрытия')
        if action == 'set_stop' and not plan['stop_price']:
            raise PlanReviewError('Цена стопа не определена')
        if action == 'set_take_profit' and not json.loads(plan['take_profits_json'] or '[]'):
            raise PlanReviewError('Цены тейк-профита не определены')
    else:
        raise PlanReviewError('Информационный сигнал нельзя подтвердить как действие')


def review_trade_plan(db, plan_id, expected_version, decision, adjustments,
                      comment, actor, idempotency_key):
    existing = db.execute(
        'SELECT id,plan_id,decision FROM approvals WHERE idempotency_key=?',
        (idempotency_key,)).fetchone()
    if existing:
        stored_decision = 'approve' if existing['decision'] == 'approved' else 'reject'
        if existing['plan_id'] != int(plan_id) or stored_decision != decision:
            raise PlanReviewError('Ключ операции уже использован для другого решения')
        return dict(existing), False
    if decision not in ('approve', 'reject'):
        raise PlanReviewError('Неизвестное решение')
    plan = db.execute('''
        SELECT p.*,s.bank_limit_usdt AS current_bank_limit,
               mc.status AS market_status,mc.reason AS market_reason,
               mc.checked_at AS market_checked_at,
               mc.contract_json AS market_contract_json,
               mc.ticker_json AS market_ticker_json
        FROM trade_plans p JOIN channel_settings s ON s.channel_id=p.channel_id
        LEFT JOIN market_checks mc ON mc.id=p.market_check_id
        WHERE p.id=?''', (plan_id,)).fetchone()
    if not plan:
        raise PlanReviewError('План не найден')
    if plan['version'] != expected_version:
        raise PlanReviewError('План уже изменён; обновите страницу или сообщение')
    if plan['status'] not in (*REVIEWABLE, 'approved'):
        raise PlanReviewError('План уже обработан или устарел')
    if decision == 'approve' and plan['status'] == 'approved':
        raise PlanReviewError('План уже подтверждён')

    before = _snapshot(plan)
    now = utc_now()
    new_version = plan['version'] + 1
    comment = (comment or '').strip()[:1000] or None
    normalized = {}

    if decision == 'reject':
        new_status = 'cancelled'
    else:
        leverage = _decimal(adjustments.get('leverage'), 'Плечо')
        if leverage is not None and leverage != leverage.to_integral_value():
            raise PlanReviewError('Плечо должно быть целым числом')
        margin = _decimal(adjustments.get('margin_usdt'), 'Маржа')
        close_percent = _decimal(
            adjustments.get('close_percent'), 'Доля закрытия', Decimal(100))
        values = {
            'effective_leverage': int(leverage) if leverage is not None else plan['effective_leverage'],
            'margin_usdt': _number(margin) if margin is not None else plan['margin_usdt'],
            'close_percent': _number(close_percent) if close_percent is not None else plan['close_percent'],
            'questions_json': '[]' if comment else plan['questions_json'],
            'target_position_id': plan['target_position_id'],
            # A manual review creates a new plan version.  Snapshot the current
            # channel limit so an old plan cannot bypass a later bank change.
            'bank_limit_snapshot_usdt': plan['current_bank_limit'],
        }
        _validate_ready(db, plan, values, comment)
        normalized = {key: value for key, value in {
            'leverage': values['effective_leverage'] if leverage is not None else None,
            'margin_usdt': values['margin_usdt'] if margin is not None else None,
            'close_percent': values['close_percent'] if close_percent is not None else None,
        }.items() if value is not None}
        updated = db.execute('''
            UPDATE trade_plans SET effective_leverage=?,margin_usdt=?,close_percent=?,
                questions_json=?,target_position_id=?,bank_limit_snapshot_usdt=?,
                status='approved',reason_code=NULL,
                version=?,updated_at=? WHERE id=? AND version=?''',
            (values['effective_leverage'], values['margin_usdt'], values['close_percent'],
             values['questions_json'], values['target_position_id'],
             values['bank_limit_snapshot_usdt'], new_version, now,
             plan_id, expected_version))
        new_status = 'approved'

    if decision == 'reject':
        updated = db.execute('''
            UPDATE trade_plans SET status='cancelled',reason_code='operator_rejected',
                version=?,updated_at=? WHERE id=? AND version=?''',
            (new_version, now, plan_id, expected_version))
    if updated.rowcount != 1:
        raise PlanReviewError('План уже изменён; обновите страницу или сообщение')
    after = {'status': new_status, 'version': new_version, **normalized}
    db.execute('''
        INSERT INTO plan_revisions(plan_id,old_version,new_version,reason,actor,before_json,created_at)
        VALUES (?,?,?,?,?,?,?)''',
        (plan_id, expected_version, new_version, f'manual_{decision}', actor,
         json.dumps(before, ensure_ascii=False, sort_keys=True), now))
    cursor = db.execute('''
        INSERT INTO approvals(plan_id,plan_version,decision,adjustments_json,comment,actor,
                              idempotency_key,created_at)
        VALUES (?,?,?,?,?,?,?,?)''',
        (plan_id, new_version, 'approved' if decision == 'approve' else 'rejected',
         json.dumps(normalized, ensure_ascii=False, sort_keys=True), comment, actor,
         idempotency_key, now))
    db.execute('''
        INSERT INTO audit_log(actor,action,entity_type,entity_id,before_json,after_json,created_at)
        VALUES (?,?,?,?,?,?,?)''',
        (actor, f'plan.{"approved" if decision == "approve" else "rejected"}',
         'trade_plan', str(plan_id), json.dumps(before, ensure_ascii=False, sort_keys=True),
         json.dumps(after, ensure_ascii=False, sort_keys=True), now))
    return {'id': cursor.lastrowid, 'plan_id': plan_id,
            'decision': 'approved' if decision == 'approve' else 'rejected'}, True
