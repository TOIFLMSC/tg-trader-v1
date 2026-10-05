"""Deterministic trade-plan projection from a validated recognition result.

The planner never calls an exchange.  It snapshots the signal and channel sizing
configuration, validates whether an action is complete, and records why it is
only a preview, needs input, or is blocked.
"""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json

from trader.correlation import AMBIGUOUS_LINK_QUESTION
from trader.migrations import utc_now


RESERVING_PLAN_STATUSES = ('ready', 'approved', 'executing')
ACTIVE_POSITION_STATUSES = ('pending', 'open', 'closing')
MANAGEMENT_ACTIONS = ('close_partial', 'close_full', 'set_stop', 'set_take_profit')


def _decimal(value):
    if value in (None, ''):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number > 0 else None


def _number(value):
    if value is None:
        return None
    text = format(value.normalize(), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _leverage(signal):
    low = _decimal(signal.get('leverage_min'))
    high = _decimal(signal.get('leverage_max')) or low
    if low is None or high is None or low > high:
        return None
    return int(((low + high) / 2).to_integral_value(rounding=ROUND_FLOOR))


def _channel_margin(settings):
    bank = _decimal(settings['bank_limit_usdt'])
    value = _decimal(settings['sizing_value'])
    if bank is None or value is None:
        return bank, None
    if settings['sizing_mode'] == 'percent':
        if value > 100:
            return bank, None
        return bank, bank * value / Decimal(100)
    if settings['sizing_mode'] == 'fixed_usdt':
        return bank, value
    return bank, None


def _active_position(db, symbol, environment=None):
    if not symbol:
        return None
    params = [symbol, *ACTIVE_POSITION_STATUSES]
    environment_clause = ''
    if environment in ('paper', 'live'):
        environment_clause = ' AND environment=?'
        params.append(environment)
    return db.execute(f'''
        SELECT * FROM positions WHERE symbol=? AND status IN (?,?,?)
        {environment_clause} ORDER BY id LIMIT 1''', params).fetchone()


def _reserved_margin(db, channel_id, environment):
    plans = db.execute('''
        SELECT margin_usdt FROM trade_plans
        WHERE channel_id=? AND environment=? AND status IN (?,?,?)''',
        (channel_id, environment, *RESERVING_PLAN_STATUSES)).fetchall()
    positions = db.execute('''
        SELECT COALESCE(allocated_margin_usdt,initial_margin_usdt) AS reserved_margin
        FROM positions
        WHERE channel_id=? AND environment=? AND status IN (?,?,?)''',
        (channel_id, environment, *ACTIVE_POSITION_STATUSES)).fetchall()
    return (sum((Decimal(row['margin_usdt']) for row in plans if row['margin_usdt']), Decimal(0))
            + sum((Decimal(row['reserved_margin']) for row in positions), Decimal(0)))


def _symbol_reserved(db, symbol, environment):
    if not symbol:
        return False
    return db.execute('''
        SELECT 1 FROM trade_plans WHERE symbol=? AND environment=?
        AND status IN (?,?,?) LIMIT 1''',
        (symbol, environment, *RESERVING_PLAN_STATUSES)).fetchone() is not None


def _plan_signal(db, event, settings, signal, environment):
    action = signal.get('action') or 'unknown'
    symbol = signal.get('symbol')
    side = signal.get('side')
    entry_kind = signal.get('entry_kind') or 'unspecified'
    entries = list(signal.get('entry_prices') or [])
    take_profits = list(signal.get('take_profits') or [])
    questions = [str(item)[:500] for item in (signal.get('questions') or [])]
    leverage = _leverage(signal)
    bank, margin = _channel_margin(settings)
    position = None
    reason = None
    status = None

    if action in ('scenario', 'report', 'unknown', 'cancel'):
        status, reason = 'informational', 'informational_signal'
    elif action in ('open', 'add'):
        missing = []
        if not symbol:
            missing.append('Нужно подтвердить тикер.')
        if not side:
            missing.append('Нужно подтвердить направление сделки.')
        if leverage is None:
            missing.append('Нужно выбрать плечо; значение по умолчанию — 1×.')
        if entry_kind == 'unspecified':
            missing.append('Нужно выбрать тип входа.')
        elif entry_kind == 'trigger_limit' and len(entries) != 1:
            missing.append('Для trigger-limit нужен один однозначный уровень входа.')
        if bank is None or margin is None:
            missing.append('Настройте банк канала и размер входа.')
        if action == 'add':
            position = _active_position(db, symbol, environment)
            if position is None:
                status, reason = 'blocked', 'no_open_position'
            elif position['channel_id'] != event['channel_id']:
                status, reason = 'blocked', 'foreign_position'
        if status is None and (missing or questions):
            status, reason = 'needs_input', 'incomplete_signal'
            questions = missing + questions
        if status is None and environment == 'recognition':
            status, reason = 'preview', 'execution_disabled'
        if status is None and _active_position(db, symbol, environment) and action == 'open':
            status, reason = 'blocked', 'symbol_has_position'
        if status is None and _symbol_reserved(db, symbol, environment) and action == 'open':
            status, reason = 'blocked', 'symbol_reserved'
        if status is None and margin is not None and bank is not None:
            if _reserved_margin(db, event['channel_id'], environment) + margin > bank:
                status, reason = 'blocked', 'bank_limit_exceeded'
        if status is None:
            status = 'ready'
    elif action in MANAGEMENT_ACTIONS:
        missing = []
        if not symbol:
            missing.append('Нужно подтвердить тикер управляемой позиции.')
        if action == 'close_partial' and _decimal(signal.get('close_percent')) is None:
            missing.append('Нужно указать долю частичного закрытия.')
        if action == 'set_stop' and _decimal(signal.get('stop_price')) is None:
            missing.append('Нужно указать цену стопа.')
        if action == 'set_take_profit' and not take_profits:
            missing.append('Нужно указать хотя бы одну цену тейк-профита.')
        position = _active_position(db, symbol, environment)
        if position is None:
            status, reason = 'blocked', 'no_open_position'
        elif position['channel_id'] != event['channel_id']:
            status, reason = 'blocked', 'foreign_position'
        else:
            # Recognition history can contain old preview-only entries and make
            # correlation look ambiguous. The executor's actual open position
            # is authoritative when ticker, side and channel agree.
            if side and side != position['side']:
                missing.append(
                    f'Распознано направление {side}, но открытая позиция — {position["side"]}.')
            else:
                questions = [item for item in questions
                             if item != AMBIGUOUS_LINK_QUESTION]
            if missing or questions:
                status, reason = 'needs_input', 'incomplete_signal'
                questions = missing + questions
            elif environment == 'recognition':
                status, reason = 'preview', 'execution_disabled'
            else:
                status = 'ready'
    else:
        status, reason = 'blocked', 'unsupported_action'

    trigger = entries[0] if entry_kind == 'trigger_limit' and len(entries) == 1 else None
    return {
        'action': action, 'symbol': symbol, 'side': side, 'order_kind': entry_kind,
        'entry_prices_json': json.dumps(entries, ensure_ascii=False),
        'trigger_price': trigger, 'limit_price': trigger,
        'effective_leverage': leverage,
        'margin_usdt': _number(margin),
        'bank_limit_snapshot_usdt': _number(bank),
        'sizing_mode': settings['sizing_mode'], 'sizing_value': settings['sizing_value'],
        'stop_price': signal.get('stop_price'),
        'take_profits_json': json.dumps(take_profits, ensure_ascii=False),
        'close_percent': signal.get('close_percent'),
        'related_message_id': signal.get('related_message_id'),
        'target_position_id': position['id'] if position else None,
        'status': status, 'reason_code': reason,
        'questions_json': json.dumps(questions, ensure_ascii=False),
        'evidence': str(signal.get('evidence') or '')[:2000],
        'signal_json': json.dumps(signal, ensure_ascii=False, sort_keys=True),
    }


def sync_trade_plans(db, event_id, analysis, environment_override=None):
    """Create an idempotent immutable plan revision for an event."""
    canonical = json.dumps(analysis, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    analysis_hash = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    existing = db.execute(
        'SELECT id FROM trade_plans WHERE event_id=? AND analysis_hash=? ORDER BY signal_index',
        (event_id, analysis_hash)).fetchall()
    if existing:
        return [row['id'] for row in existing]

    event = db.execute('''
        SELECT e.id,e.channel_id,e.message,c.title,
               s.bank_limit_usdt,s.sizing_mode,s.sizing_value
        FROM events e JOIN channels c ON c.id=e.channel_id
        JOIN channel_settings s ON s.channel_id=c.id WHERE e.id=?''', (event_id,)).fetchone()
    if event is None:
        raise ValueError('Event does not exist')
    environment_row = db.execute(
        "SELECT value FROM app_settings WHERE key='execution_mode'").fetchone()
    environment = environment_override or (environment_row['value'] if environment_row else 'recognition')
    if environment not in ('recognition', 'paper', 'live'):
        raise ValueError('Invalid execution mode')

    revision = db.execute(
        'SELECT COALESCE(MAX(revision),0)+1 FROM trade_plans WHERE event_id=?',
        (event_id,)).fetchone()[0]
    db.execute('''
        UPDATE trade_plans SET status='superseded',updated_at=?
        WHERE event_id=? AND status IN (
            'preview','ready','needs_input','blocked','informational','approved')''',
        (utc_now(), event_id))

    now = utc_now()
    ids = []
    settings = {
        'bank_limit_usdt': event['bank_limit_usdt'],
        'sizing_mode': event['sizing_mode'], 'sizing_value': event['sizing_value'],
    }
    for index, signal in enumerate(analysis.get('signals') or []):
        plan = _plan_signal(db, event, settings, signal, environment)
        columns = ['event_id', 'channel_id', 'signal_index', 'analysis_hash', 'revision',
                   'source_message', 'environment', 'created_at', 'updated_at', *plan.keys()]
        values = [event_id, event['channel_id'], index, analysis_hash, revision,
                  event['message'], environment, now, now, *plan.values()]
        placeholders = ','.join('?' for _ in columns)
        cursor = db.execute(
            f'INSERT INTO trade_plans({",".join(columns)}) VALUES ({placeholders})', values)
        ids.append(cursor.lastrowid)
    return ids
