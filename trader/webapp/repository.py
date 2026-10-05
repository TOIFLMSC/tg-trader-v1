from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3

from bootstrap import ROOT
from trader.migrations import utc_now
from trader.store import Store


class SettingsError(ValueError):
    pass


class SettingsConflict(RuntimeError):
    pass


PLAN_STATUS_LABELS = {
    'preview': 'Предпросмотр', 'ready': 'Готов к подтверждению',
    'needs_input': 'Нужно уточнение', 'blocked': 'Заблокирован',
    'informational': 'Информация', 'approved': 'Подтверждён',
    'executing': 'Исполняется', 'executed': 'Исполнен', 'cancelled': 'Отменён',
    'superseded': 'Устарел', 'failed': 'Ошибка',
}
PLAN_REASON_LABELS = {
    'execution_disabled': 'Режим распознавания: исполнение отключено.',
    'informational_signal': 'Сообщение не является исполняемым торговым действием.',
    'incomplete_signal': 'Для плана не хватает обязательных параметров.',
    'no_open_position': 'У этого канала нет открытой позиции по указанному тикеру.',
    'foreign_position': 'Позиция принадлежит другому каналу.',
    'symbol_has_position': 'По монете уже есть активная позиция.',
    'symbol_reserved': 'Монета уже зарезервирована более ранним планом.',
    'bank_limit_exceeded': 'Свободного лимита банка канала недостаточно.',
    'unsupported_action': 'Этот тип действия пока не поддержан планировщиком.',
    'operator_rejected': 'План пропущен владельцем.',
    'paper_execution_failed': 'Виртуальное исполнение завершилось ошибкой.',
    'paper_order_cancelled': 'Ожидающая виртуальная заявка отменена владельцем.',
    'live_reapproval_required': (
        'Live-защёлка была снята. После повторной активации подтвердите план заново.'),
}
LIVE_DISARM_REASON_LABELS = {
    'MEXC private request transport failure': (
        'Не удалось связаться с приватным API MEXC во время фоновой сверки.'),
    'Live latch is not active': 'Live-защёлка не активна.',
}
MARKET_STATUS_LABELS = {
    'valid': 'MEXC: готово',
    'specs_only': 'MEXC: нужны параметры',
    'stale_review': 'MEXC: сигнал устарел',
    'scenario_finished': 'MEXC: сценарий завершён',
    'contract_not_found': 'MEXC: контракта нет',
    'inactive': 'MEXC: контракт неактивен',
    'api_disabled': 'MEXC: API недоступен',
    'isolated_unsupported': 'MEXC: нет isolated',
    'unsupported_contract': 'MEXC: тип не поддержан',
    'leverage_unsupported': 'MEXC: плечо недоступно',
    'below_min_volume': 'MEXC: объём меньше минимума',
    'above_max_volume': 'MEXC: объём выше максимума',
    'price_step_mismatch': 'MEXC: неверный шаг цены',
    'error': 'MEXC: ошибка проверки',
}
RECOVERY_REASON_LABELS = {
    'overlapping_candle': (
        'Защитный уровень пересечён в минутной свече, которая началась до последней '
        'сохранённой котировки. Порядок событий неизвестен.'),
    'ambiguous_protection_order': (
        'В одной минутной свече пересечено несколько защитных уровней. Их порядок неизвестен.'),
    'history_window_exceeded': (
        'Перерыв длиннее доступного окна минутных свечей. Автоматически восстановить путь цены нельзя.'),
}


def connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    return db


def safe_json(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def format_timestamp(value):
    parsed = parse_timestamp(value)
    if not parsed:
        return '—'
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime('%d.%m.%Y %H:%M:%S')


def channel_rows(db):
    return [dict(row) for row in db.execute('''
        SELECT c.id,c.telegram_id,c.title,c.enabled,c.connection_status,c.last_error,c.checked_at,
               c.created_at,c.updated_at,
               s.bank_limit_usdt,s.sizing_mode,s.sizing_value,s.version AS settings_version,
               COUNT(e.id) AS event_count,
               COALESCE(SUM(CASE WHEN e.status='done' THEN 1 ELSE 0 END),0) AS done_count,
               COALESCE(SUM(CASE WHEN e.status='failed' THEN 1 ELSE 0 END),0) AS failed_count,
               MAX(COALESCE(e.received_at,e.telegram_date)) AS last_event_at
        FROM channels c
        LEFT JOIN channel_settings s ON s.channel_id=c.id
        LEFT JOIN events e ON e.channel_id=c.id
        GROUP BY c.id,c.telegram_id,c.title,c.enabled,c.connection_status,c.last_error,c.checked_at,
                 c.created_at,c.updated_at,
                 s.bank_limit_usdt,s.sizing_mode,s.sizing_value,s.version
        ORDER BY c.enabled DESC,c.title
        ''').fetchall()]


def dashboard_data(path):
    db = connect(path)
    try:
        settings = {row['key']: row['value'] for row in db.execute(
            'SELECT key,value FROM app_settings')}
        counts = {row['status']: row['count'] for row in db.execute(
            'SELECT status,COUNT(*) AS count FROM events GROUP BY status')}
        now = datetime.now(timezone.utc)
        services = []
        for row in db.execute('SELECT * FROM service_heartbeats ORDER BY service_name'):
            item = dict(row)
            updated = parse_timestamp(item['updated_at'])
            age = (now - updated).total_seconds() if updated else None
            item['online'] = item['status'] in ('starting', 'running') and age is not None and age <= 15
            item['age_seconds'] = round(age) if age is not None else None
            item['details'] = safe_json(item['details'], {})
            services.append(item)
        last_event = db.execute('''
            SELECT e.id,e.message,e.status,e.error,e.telegram_date,e.received_at,c.title AS channel_title
            FROM events e LEFT JOIN channels c ON c.id=e.channel_id
            ORDER BY e.id DESC LIMIT 1''').fetchone()
        month = now.strftime('%Y-%m')
        spent = db.execute(
            'SELECT COALESCE(SUM(cost),0) FROM usage WHERE month=?', (month,)).fetchone()[0]
        schema_version = db.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
        plan_counts = {row['status']: row['count'] for row in db.execute(
            'SELECT status,COUNT(*) AS count FROM trade_plans GROUP BY status')}
        position_counts = {row['status']: row['count'] for row in db.execute(
            'SELECT status,COUNT(*) AS count FROM positions GROUP BY status')}
        return {
            'settings': settings,
            'counts': counts,
            'channels': channel_rows(db),
            'services': services,
            'last_event': dict(last_event) if last_event else None,
            'spent': spent,
            'schema_version': schema_version,
            'plan_counts': plan_counts,
            'position_counts': position_counts,
        }
    finally:
        db.close()


def list_channels(path):
    db = connect(path)
    try:
        return channel_rows(db)
    finally:
        db.close()


def _decimal(value, label, allow_empty=False):
    raw = (value or '').strip()
    if allow_empty and not raw:
        return None
    if not raw or ',' in raw:
        raise SettingsError(f'{label}: укажите положительное число через точку.')
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise SettingsError(f'{label}: некорректное число.') from None
    if not number.is_finite() or number <= 0:
        raise SettingsError(f'{label}: значение должно быть больше нуля.')
    if number > Decimal('1000000000'):
        raise SettingsError(f'{label}: значение слишком велико.')
    normalized = format(number.normalize(), 'f')
    return normalized


def update_channel_settings(path, channel_id, bank_limit, sizing_mode, sizing_value,
                            expected_version, actor='web-local'):
    if sizing_mode not in ('percent', 'fixed_usdt'):
        raise SettingsError('Неизвестный способ расчёта размера входа.')
    bank = _decimal(bank_limit, 'Банк канала', allow_empty=True)
    size = _decimal(sizing_value, 'Размер входа')
    if sizing_mode == 'percent' and Decimal(size) > 100:
        raise SettingsError('Процент входа не может превышать 100%.')
    if sizing_mode == 'fixed_usdt' and bank is not None and Decimal(size) > Decimal(bank):
        raise SettingsError('Фиксированный вход не может превышать банк канала.')
    try:
        version = int(expected_version)
    except (TypeError, ValueError):
        raise SettingsError('Версия настроек не указана.') from None

    db = connect(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''
                SELECT c.title,s.bank_limit_usdt,s.sizing_mode,s.sizing_value,s.version
                FROM channels c JOIN channel_settings s ON s.channel_id=c.id
                WHERE c.id=?''', (channel_id,)).fetchone()
            if not row:
                raise SettingsError('Канал не найден.')
            if row['version'] != version:
                raise SettingsConflict('Настройки уже изменились в другой вкладке. Обновите страницу.')
            before = {'bank_limit_usdt': row['bank_limit_usdt'], 'sizing_mode': row['sizing_mode'],
                      'sizing_value': row['sizing_value'], 'version': row['version']}
            now = utc_now()
            current = db.execute('''
                UPDATE channel_settings
                SET bank_limit_usdt=?,sizing_mode=?,sizing_value=?,version=version+1,updated_at=?
                WHERE channel_id=? AND version=?''',
                (bank, sizing_mode, size, now, channel_id, version))
            if current.rowcount != 1:
                raise SettingsConflict('Настройки изменились до сохранения. Обновите страницу.')
            after = {'bank_limit_usdt': bank, 'sizing_mode': sizing_mode,
                     'sizing_value': size, 'version': version + 1}
            db.execute('''
                INSERT INTO audit_log(actor,action,entity_type,entity_id,before_json,after_json,created_at)
                VALUES (?,?,?,?,?,?,?)''',
                (actor, 'channel_settings.updated', 'channel', str(channel_id),
                 json.dumps(before, ensure_ascii=False, sort_keys=True),
                 json.dumps(after, ensure_ascii=False, sort_keys=True), now))
            return after
    finally:
        db.close()


def _command_key(value):
    raw = (value or '').strip()
    if not 20 <= len(raw) <= 128 or not all(char.isalnum() or char in '-_.' for char in raw):
        raise SettingsError('Некорректный ключ управляющей команды. Обновите страницу.')
    return raw


def enqueue_control(path, kind, payload, idempotency_key, actor='web-local'):
    allowed = {'recognition.pause', 'recognition.resume', 'approval.set',
               'execution.set', 'channel.add', 'channel.monitor', 'paper_order.cancel',
               'paper_position.resume_recovery', 'live.check', 'live.activate',
               'live.disarm', 'live_order.cancel'}
    if kind not in allowed:
        raise SettingsError('Неизвестная управляющая команда.')
    key = _command_key(idempotency_key)
    store = Store(path)
    try:
        command_id, created = store.queue_control(kind, payload, key, actor)
        row = store.db.execute(
            'SELECT status FROM control_commands WHERE id=?', (command_id,)).fetchone()
        return {'id': command_id, 'status': row['status'], 'created': created}
    finally:
        store.db.close()


def control_data(path):
    db = connect(path)
    try:
        settings = {row['key']: {'value': row['value'], 'version': row['version'],
                                 'updated_at': row['updated_at']}
                    for row in db.execute('SELECT * FROM app_settings')}
        commands = []
        for row in db.execute('SELECT * FROM control_commands ORDER BY id DESC LIMIT 20'):
            item = dict(row)
            item['payload_data'] = safe_json(item['payload'], {})
            commands.append(item)
        audit = []
        for row in db.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 20'):
            item = dict(row)
            item['after_data'] = safe_json(item['after_json'], {})
            audit.append(item)
        reader = db.execute(
            "SELECT * FROM service_heartbeats WHERE service_name='recognition'").fetchone()
        reader_online = False
        if reader:
            updated = parse_timestamp(reader['updated_at'])
            reader_online = (reader['status'] in ('starting', 'running') and updated is not None
                             and (datetime.now(timezone.utc) - updated).total_seconds() <= 15)
        snapshot_row = db.execute(
            'SELECT * FROM live_snapshots ORDER BY id DESC LIMIT 1').fetchone()
        live_snapshot = dict(snapshot_row) if snapshot_row else None
        live_snapshot_fresh = False
        if live_snapshot:
            live_snapshot['positions'] = safe_json(live_snapshot.pop('positions_json'), [])
            live_snapshot['orders'] = safe_json(live_snapshot.pop('orders_json'), [])
            checked = parse_timestamp(live_snapshot.get('checked_at'))
            age = ((datetime.now(timezone.utc) - checked).total_seconds()
                   if checked is not None else None)
            live_snapshot['age_seconds'] = max(0, round(age)) if age is not None else None
            live_snapshot_fresh = (
                live_snapshot.get('status') == 'ready' and age is not None and age <= 300)
        disarm = next((item for item in audit if item['action'] == 'live.disarmed'), None)
        disarm_reason = (disarm or {}).get('after_data', {}).get('reason')
        return {
            'settings': settings,
            'paused': settings.get('recognition_paused', {}).get('value', 'false') == 'true',
            'commands': commands,
            'audit': audit,
            'reader_online': reader_online,
            'live_snapshot': live_snapshot,
            'live_snapshot_fresh': live_snapshot_fresh,
            'live_disarm_reason': LIVE_DISARM_REASON_LABELS.get(
                disarm_reason, disarm_reason),
            'pending_count': db.execute(
                "SELECT COUNT(*) FROM control_commands WHERE status='pending'").fetchone()[0],
        }
    finally:
        db.close()


def _telegram_url(telegram_id, message):
    raw = str(abs(int(telegram_id)))
    if raw.startswith('100'):
        return f'https://t.me/c/{raw[3:]}/{message}'
    return None


def parse_event(row):
    item = dict(row)
    payload = safe_json(item.get('payload'), {})
    analysis = safe_json(item.get('analysis'), {})
    messages = payload.get('messages') or []
    main = messages[0] if messages else {}
    signals = analysis.get('signals') or []
    symbols = sorted({str(signal.get('symbol')) for signal in signals if signal.get('symbol')})
    actions = sorted({str(signal.get('action')) for signal in signals if signal.get('action')})
    item.update({
        'payload_data': payload,
        'analysis_data': analysis,
        'main': main,
        'parents': payload.get('parents') or [],
        'signals': signals,
        'symbols': symbols,
        'actions': actions,
        'summary': analysis.get('summary'),
        'text': main.get('text') or '',
        'media': main.get('media') or [],
        'display_date': format_timestamp(main.get('date') or item.get('telegram_date') or item.get('received_at')),
        'telegram_url': _telegram_url(item['telegram_id'], item['message']),
    })
    return item


def list_events(path, channel_id=None, status=None, action=None, symbol=None, page=1, per_page=10):
    clauses, params = [], []
    if channel_id:
        clauses.append('e.channel_id=?')
        params.append(channel_id)
    if status in ('pending', 'done', 'failed'):
        clauses.append('e.status=?')
        params.append(status)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    db = connect(path)
    try:
        rows = db.execute(f'''
            SELECT e.*,c.title AS channel_title,c.telegram_id
            FROM events e JOIN channels c ON c.id=e.channel_id
            {where} ORDER BY e.id DESC''', params).fetchall()
        events = [parse_event(row) for row in rows]
        action = (action or '').strip().lower()
        symbol = (symbol or '').strip().upper()
        if action:
            events = [event for event in events if action in event['actions']]
        if symbol:
            events = [event for event in events if any(symbol in value for value in event['symbols'])]
        total = len(events)
        per_page = max(1, min(int(per_page), 50))
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(int(page), pages))
        start = (page - 1) * per_page
        return {'events': events[start:start + per_page], 'total': total,
                'page': page, 'pages': pages, 'per_page': per_page}
    finally:
        db.close()


def event_detail(path, event_id):
    db = connect(path)
    try:
        row = db.execute('''
            SELECT e.*,c.title AS channel_title,c.telegram_id
            FROM events e JOIN channels c ON c.id=e.channel_id WHERE e.id=?''', (event_id,)).fetchone()
        if not row:
            return None
        event = parse_event(row)
        event['revisions'] = [dict(revision) for revision in db.execute('''
            SELECT id,reason,old_status,old_analysis,old_error,created_at
            FROM analysis_revisions WHERE event_id=? ORDER BY id DESC''', (event_id,)).fetchall()]
        for revision in event['revisions']:
            revision['old_analysis_data'] = safe_json(revision['old_analysis'], {})
        event['trade_plans'] = [_parse_plan(plan) for plan in db.execute('''
            SELECT p.*,c.title AS channel_title FROM trade_plans p
            JOIN channels c ON c.id=p.channel_id
            WHERE p.event_id=? ORDER BY p.revision DESC,p.signal_index''',
            (event_id,)).fetchall()]
        for plan in event['trade_plans']:
            plan['approvals'] = [dict(item) for item in db.execute('''
                SELECT * FROM approvals WHERE plan_id=? ORDER BY id DESC''',
                (plan['id'],)).fetchall()]
        return event
    finally:
        db.close()


def media_file(path, event_id, index):
    event = event_detail(path, event_id)
    if not event or index < 0 or index >= len(event['media']):
        return None
    descriptor = event['media'][index]
    relative = descriptor.get('path')
    if not relative:
        return None
    media_root = (ROOT / 'data/media').resolve()
    candidate = (ROOT / relative).resolve()
    if (not candidate.is_relative_to(media_root) or not candidate.is_file()
            or candidate.stat().st_size > 8_000_000):
        return None
    mime_by_suffix = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                      '.png': 'image/png', '.webp': 'image/webp'}
    mime = mime_by_suffix.get(candidate.suffix.lower())
    if not mime:
        return None
    expected_hash = descriptor.get('sha256')
    if expected_hash and hashlib.sha256(candidate.read_bytes()).hexdigest() != expected_hash:
        return None
    return candidate, mime


EQUITY_PERIODS = {
    'days': ('Дни · последние 30', 30),
    'months': ('Месяцы · последние 12', 12),
    'years': ('Годы · последние 10', 10),
}


def normalize_equity_period(value):
    return value if value in EQUITY_PERIODS else 'days'


def _shift_month(year, month, offset):
    absolute = year * 12 + month - 1 + offset
    return absolute // 12, absolute % 12 + 1


def _equity_buckets(period, now):
    now = now.astimezone(timezone.utc)
    if period == 'days':
        first = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=29)
        starts = [first + timedelta(days=index) for index in range(30)]
        keys = [item.date() for item in starts]
        labels = [item.strftime('%d.%m.%y') for item in starts]
        event_key = lambda value: value.date()
        end = starts[-1] + timedelta(days=1)
    elif period == 'months':
        first_year, first_month = _shift_month(now.year, now.month, -11)
        starts = []
        for index in range(12):
            year, month = _shift_month(first_year, first_month, index)
            starts.append(datetime(year, month, 1, tzinfo=timezone.utc))
        keys = [(item.year, item.month) for item in starts]
        labels = [item.strftime('%m.%Y') for item in starts]
        event_key = lambda value: (value.year, value.month)
        next_year, next_month = _shift_month(starts[-1].year, starts[-1].month, 1)
        end = datetime(next_year, next_month, 1, tzinfo=timezone.utc)
    else:
        first = datetime(now.year - 9, 1, 1, tzinfo=timezone.utc)
        starts = [datetime(first.year + index, 1, 1, tzinfo=timezone.utc)
                  for index in range(10)]
        keys = [item.year for item in starts]
        labels = [str(item.year) for item in starts]
        event_key = lambda value: value.year
        end = datetime(starts[-1].year + 1, 1, 1, tzinfo=timezone.utc)
    return starts[0], end, keys, labels, event_key


def _axis_money(value):
    value = float(value)
    return f'{value:+.2f} USDT' if value else '0.00 USDT'


def _paper_equity_curve(db, channel_id=None, period=None, now=None):
    fill_params = (channel_id,) if channel_id is not None else ()
    fill_where = 'WHERE p.channel_id=?' if channel_id is not None else ''
    events = []
    for row in db.execute(f'''SELECT f.created_at,o.intent,f.fee_usdt,
            f.realized_pnl_usdt,p.channel_id
        FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id
        JOIN positions p ON p.id=f.position_id {fill_where}''', fill_params):
        delta = (-Decimal(row['fee_usdt']) if row['intent'] in ('open', 'add')
                 else Decimal(row['realized_pnl_usdt']))
        timestamp = parse_timestamp(row['created_at'])
        if timestamp:
            events.append((timestamp.astimezone(timezone.utc), delta))
    funding_params = (channel_id,) if channel_id is not None else ()
    funding_where = 'WHERE p.channel_id=?' if channel_id is not None else ''
    for row in db.execute(f'''SELECT f.settle_time,f.amount_usdt
        FROM paper_funding f JOIN positions p ON p.id=f.position_id {funding_where}''',
                          funding_params):
        timestamp = parse_timestamp(row['settle_time'])
        if timestamp:
            events.append((timestamp.astimezone(timezone.utc), Decimal(row['amount_usdt'])))
    events.sort(key=lambda item: item[0])

    selected_period = normalize_equity_period(period) if period else None
    selected_events = events
    labels = []
    if selected_period:
        start, end, keys, labels, event_key = _equity_buckets(
            selected_period, now or datetime.now(timezone.utc))
        cumulative = sum((delta for timestamp, delta in events if timestamp < start), Decimal(0))
        deltas = {key: Decimal(0) for key in keys}
        selected_events = []
        for timestamp, delta in events:
            if start <= timestamp < end:
                key = event_key(timestamp)
                if key in deltas:
                    deltas[key] += delta
                    selected_events.append((timestamp, delta))
        values = []
        for key in keys:
            cumulative += deltas[key]
            values.append(cumulative)
    else:
        cumulative = Decimal(0)
        values = [Decimal(0)]
        for _, delta in events:
            cumulative += delta
            values.append(cumulative)

    peak = values[0] if values else Decimal(0)
    max_drawdown = Decimal(0)
    for value in values:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)
    actual_low, actual_high = min(values), max(values)
    low, high = min(Decimal(0), actual_low), max(Decimal(0), actual_high)
    if low == high:
        low, high = Decimal('-1'), Decimal('1')
    width, height = 820, 270
    left, right, top, bottom = 88, 16, 16, 48
    plot_width, plot_height = width - left - right, height - top - bottom
    spread = high - low
    points = []
    for index, value in enumerate(values):
        x = left if len(values) == 1 else left + index * plot_width / (len(values) - 1)
        y = top + float(high - value) * plot_height / float(spread)
        points.append(f'{x:.1f},{y:.1f}')
    zero_y = top + float(high) * plot_height / float(spread)
    y_ticks = []
    for index in range(5):
        value = high - spread * Decimal(index) / Decimal(4)
        y_ticks.append({
            'y': f'{top + index * plot_height / 4:.1f}',
            'label': _axis_money(value),
        })
    x_ticks = []
    if labels:
        tick_count = min(6, len(labels))
        indices = sorted({round(index * (len(labels) - 1) / (tick_count - 1))
                          for index in range(tick_count)}) if tick_count > 1 else [0]
        for index in indices:
            x = left if len(labels) == 1 else left + index * plot_width / (len(labels) - 1)
            x_ticks.append({'x': f'{x:.1f}', 'label': labels[index]})
    return {
        'points': ' '.join(points), 'events': len(selected_events),
        'current': float(cumulative), 'minimum': float(actual_low),
        'maximum': float(actual_high),
        'max_drawdown': float(max_drawdown), 'zero_y': f'{zero_y:.1f}',
        'y_ticks': y_ticks, 'x_ticks': x_ticks,
        'period': selected_period, 'period_label': (
            EQUITY_PERIODS[selected_period][0] if selected_period else 'За всё время'),
    }


def recognition_stats(path, equity_period='days'):
    db = connect(path)
    try:
        channels = {row['id']: dict(row) for row in db.execute(
            'SELECT id,title,telegram_id FROM channels ORDER BY title')}
        for channel in channels.values():
            channel.update(total=0, done=0, failed=0, pending=0,
                           actions=Counter(), symbols=Counter(), questions=0)
        global_actions, global_symbols = Counter(), Counter()
        for row in db.execute('''
            SELECT e.*,c.title AS channel_title,c.telegram_id
            FROM events e JOIN channels c ON c.id=e.channel_id ORDER BY e.id'''):
            event = parse_event(row)
            channel = channels[event['channel_id']]
            channel['total'] += 1
            channel[event['status']] += 1
            for signal in event['signals']:
                action = signal.get('action') or 'unknown'
                channel['actions'][action] += 1
                global_actions[action] += 1
                if signal.get('symbol'):
                    channel['symbols'][signal['symbol']] += 1
                    global_symbols[signal['symbol']] += 1
                channel['questions'] += len(signal.get('questions') or [])
        usage = [dict(row) for row in db.execute('''
            SELECT month,ROUND(SUM(cost),6) AS cost,
                   SUM(COALESCE(input_tokens,0)) AS input_tokens,
                   SUM(COALESCE(output_tokens,0)) AS output_tokens
            FROM usage GROUP BY month ORDER BY month DESC''').fetchall()]
        paper = [dict(row) for row in db.execute('''
            SELECT c.id AS channel_id,c.title,c.telegram_id,s.bank_limit_usdt,
                   COUNT(p.id) AS positions,
                   COALESCE(SUM(CASE WHEN p.status='open' THEN 1 ELSE 0 END),0) AS open_count,
                   COALESCE(SUM(CASE WHEN p.status IN ('closed','liquidated') THEN 1 ELSE 0 END),0) AS closed_count,
                   COALESCE(SUM(CASE WHEN p.status IN ('closed','liquidated')
                                     AND CAST(p.realized_pnl_usdt AS REAL)>0 THEN 1 ELSE 0 END),0) AS winning_count,
                   COALESCE(SUM(CAST(p.realized_pnl_usdt AS REAL)),0) AS realized_pnl,
                   COALESCE(SUM(CASE WHEN p.status='open' THEN CAST(p.unrealized_pnl_usdt AS REAL) ELSE 0 END),0) AS unrealized_pnl,
                   COALESCE(SUM(CAST(p.entry_fees_usdt AS REAL)+CAST(p.exit_fees_usdt AS REAL)),0) AS fees,
                   COALESCE(SUM(CAST(p.funding_pnl_usdt AS REAL)),0) AS funding,
                   COALESCE(SUM(CASE WHEN p.status='open' THEN
                       CAST(COALESCE(p.allocated_margin_usdt,p.initial_margin_usdt) AS REAL)
                       ELSE 0 END),0) AS occupied_margin
            FROM channels c LEFT JOIN positions p ON p.channel_id=c.id AND p.environment='paper'
            LEFT JOIN channel_settings s ON s.channel_id=c.id
            GROUP BY c.id,c.title,c.telegram_id,s.bank_limit_usdt ORDER BY c.title''').fetchall()]
        for row in paper:
            row['win_rate'] = (100 * row['winning_count'] / row['closed_count']
                               if row['closed_count'] else 0)
            row['net_pnl'] = row['realized_pnl'] + row['unrealized_pnl']
            bank = float(row['bank_limit_usdt']) if row['bank_limit_usdt'] else 0
            row['return_percent'] = 100 * row['net_pnl'] / bank if bank else 0
            row['max_drawdown'] = _paper_equity_curve(db, row['channel_id'])['max_drawdown']
        closed = sum(row['closed_count'] for row in paper)
        wins = sum(row['winning_count'] for row in paper)
        realized = sum(row['realized_pnl'] for row in paper)
        unrealized = sum(row['unrealized_pnl'] for row in paper)
        fees = sum(row['fees'] for row in paper)
        funding = sum(row['funding'] for row in paper)
        bank = sum(float(row['bank_limit_usdt']) for row in paper if row['bank_limit_usdt'])
        all_time_curve = _paper_equity_curve(db)
        paper_summary = {
            'positions': sum(row['positions'] for row in paper),
            'open_count': sum(row['open_count'] for row in paper),
            'closed_count': closed, 'winning_count': wins,
            'win_rate': 100 * wins / closed if closed else 0,
            'realized_pnl': realized, 'unrealized_pnl': unrealized,
            'net_pnl': realized + unrealized, 'fees': fees, 'funding': funding,
            'occupied_margin': sum(row['occupied_margin'] for row in paper),
            'bank': bank, 'return_percent': 100 * (realized + unrealized) / bank if bank else 0,
            'max_drawdown': all_time_curve['max_drawdown'],
        }
        funding_rows = [dict(row) for row in db.execute('''SELECT f.*,c.title AS channel_title
            FROM paper_funding f JOIN positions p ON p.id=f.position_id
            JOIN channels c ON c.id=p.channel_id
            ORDER BY f.settle_time DESC LIMIT 30''').fetchall()]
        return {'channels': list(channels.values()), 'actions': global_actions.most_common(),
                'symbols': global_symbols.most_common(12), 'usage': usage, 'paper': paper,
                'paper_summary': paper_summary,
                'equity_curve': _paper_equity_curve(
                    db, period=normalize_equity_period(equity_period)),
                'equity_periods': [
                    {'value': value, 'label': values[0],
                     'selected': value == normalize_equity_period(equity_period)}
                    for value, values in EQUITY_PERIODS.items()],
                'funding_rows': funding_rows}
    finally:
        db.close()


def _parse_plan(row):
    item = dict(row)
    item['entry_prices'] = safe_json(item.get('entry_prices_json'), [])
    item['take_profits'] = safe_json(item.get('take_profits_json'), [])
    item['questions'] = safe_json(item.get('questions_json'), [])
    item['signal'] = safe_json(item.get('signal_json'), {})
    item['status_label'] = PLAN_STATUS_LABELS.get(item['status'], item['status'])
    item['reason_text'] = PLAN_REASON_LABELS.get(item.get('reason_code'), item.get('reason_code'))
    item['market_contract'] = safe_json(item.get('market_contract_json'), {})
    item['market_ticker'] = safe_json(item.get('market_ticker_json'), {})
    item['market_candles'] = safe_json(item.get('market_candles_json'), {})
    item['market_status_label'] = MARKET_STATUS_LABELS.get(
        item.get('market_status'), item.get('market_status'))
    requires_market_check = item.get('action') in ('open', 'add')
    item['market_allows_approval'] = (
        not requires_market_check
        or item.get('market_status') in ('valid', 'specs_only', 'stale_review')
    )
    return item


def _display_decimal(value):
    if value in (None, ''):
        return None
    number = Decimal(str(value))
    text = format(number.normalize(), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _pagination(total, page, per_page=10):
    per_page = max(1, min(int(per_page), 50))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page), pages))
    offset = (page - 1) * per_page
    return {
        'page': page, 'pages': pages, 'per_page': per_page, 'total': total,
        'offset': offset, 'start': offset + 1 if total else 0,
        'end': min(offset + per_page, total),
    }


def trade_data(path, positions_page=1, orders_page=1, plans_page=1, per_page=10):
    db = connect(path)
    try:
        settings = {row['key']: row['value'] for row in db.execute(
            'SELECT key,value FROM app_settings')}
        positions_pagination = _pagination(
            db.execute('SELECT COUNT(*) FROM positions').fetchone()[0],
            positions_page, per_page)
        orders_pagination = _pagination(
            db.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],
            orders_page, per_page)
        plans_pagination = _pagination(
            db.execute('SELECT COUNT(*) FROM trade_plans').fetchone()[0],
            plans_page, per_page)
        plans = [_parse_plan(row) for row in db.execute('''
            SELECT p.*,c.title AS channel_title,c.telegram_id,e.message,
                   mc.status AS market_status,mc.reason AS market_reason,
                   mc.mexc_symbol,mc.current_price,mc.bid_price,mc.ask_price,
                   mc.estimated_contracts,mc.estimated_base_quantity,
                   mc.estimated_notional_usdt,mc.observed_high,mc.observed_low,
                   mc.signal_age_seconds,mc.checked_at AS market_checked_at,
                   mc.contract_json AS market_contract_json,
                   mc.ticker_json AS market_ticker_json,
                   mc.candles_json AS market_candles_json
            FROM trade_plans p JOIN channels c ON c.id=p.channel_id
            JOIN events e ON e.id=p.event_id
            LEFT JOIN market_checks mc ON mc.id=p.market_check_id
            ORDER BY p.id DESC LIMIT ? OFFSET ?''', (
            plans_pagination['per_page'], plans_pagination['offset'])).fetchall()]
        for plan in plans:
            plan['approvals'] = [dict(item) for item in db.execute('''
                SELECT * FROM approvals WHERE plan_id=? ORDER BY id DESC''',
                (plan['id'],)).fetchall()]
            plan['last_approval'] = plan['approvals'][0] if plan['approvals'] else None
        positions = [dict(row) for row in db.execute('''
            SELECT p.*,c.title AS channel_title FROM positions p
            JOIN channels c ON c.id=p.channel_id ORDER BY p.id DESC LIMIT ? OFFSET ?''', (
            positions_pagination['per_page'], positions_pagination['offset'])).fetchall()]
        for position in positions:
            position['take_profits'] = safe_json(position.get('take_profits_json'), [])
            position['recovery_options'] = safe_json(
                position.get('recovery_options_json'), [])
            position['recovery_reason_text'] = RECOVERY_REASON_LABELS.get(
                position.get('recovery_reason'), position.get('recovery_reason') or '')
            position['total_fees_usdt'] = _display_decimal(
                Decimal(position.get('entry_fees_usdt') or '0')
                + Decimal(position.get('exit_fees_usdt') or '0'))
        orders = [dict(row) for row in db.execute('''
            SELECT o.*,c.title AS channel_title FROM paper_orders o
            LEFT JOIN trade_plans p ON p.id=o.plan_id
            LEFT JOIN channels c ON c.id=COALESCE(p.channel_id,
                (SELECT channel_id FROM positions WHERE id=o.position_id))
            ORDER BY o.id DESC LIMIT ? OFFSET ?''', (
            orders_pagination['per_page'], orders_pagination['offset'])).fetchall()]
        for order in orders:
            order['cancellable'] = order['status'] in ('pending_trigger', 'open')
        live_orders = [dict(row) for row in db.execute('''
            SELECT o.*,c.title AS channel_title FROM live_orders o
            LEFT JOIN trade_plans p ON p.id=o.plan_id
            LEFT JOIN channels c ON c.id=p.channel_id
            ORDER BY o.id DESC LIMIT 10''').fetchall()]
        for order in live_orders:
            order['cancellable'] = order['status'] in (
                'prepared', 'pending_trigger', 'open', 'partially_filled', 'unknown')
        plan_counts = {row['status']: row['count'] for row in db.execute('''
            SELECT status,COUNT(*) AS count FROM trade_plans
            WHERE status!='superseded' GROUP BY status''')}
        position_counts = {row['status']: row['count'] for row in db.execute(
            'SELECT status,COUNT(*) AS count FROM positions GROUP BY status')}
        banks = []
        for row in db.execute('''
            SELECT c.id,c.title,c.telegram_id,s.bank_limit_usdt,s.sizing_mode,s.sizing_value
            FROM channels c JOIN channel_settings s ON s.channel_id=c.id ORDER BY c.title'''):
            item = dict(row)
            normalized_limit = _decimal(
                item['bank_limit_usdt'], 'Банк', allow_empty=True)
            limit = Decimal(normalized_limit) if normalized_limit is not None else None
            plan_margins = db.execute('''
                SELECT margin_usdt FROM trade_plans WHERE channel_id=?
                AND environment IN ('paper','live')
                AND status IN ('ready','approved','executing')''', (item['id'],)).fetchall()
            position_margins = db.execute('''
                SELECT COALESCE(allocated_margin_usdt,initial_margin_usdt) FROM positions WHERE channel_id=?
                AND status IN ('pending','open','closing')''', (item['id'],)).fetchall()
            reserved = (
                sum((Decimal(value[0]) for value in plan_margins if value[0]), Decimal(0))
                + sum((Decimal(value[0]) for value in position_margins), Decimal(0)))
            item['reserved_usdt'] = _display_decimal(reserved)
            item['available_usdt'] = _display_decimal(max(limit - reserved, Decimal(0))) if limit else None
            banks.append(item)
        return {
            'settings': settings, 'plans': plans, 'positions': positions,
            'orders': orders, 'live_orders': live_orders,
            'plan_counts': plan_counts, 'position_counts': position_counts, 'banks': banks,
            'positions_pagination': positions_pagination,
            'orders_pagination': orders_pagination,
            'plans_pagination': plans_pagination,
        }
    finally:
        db.close()


def review_plan(path, plan_id, expected_version, decision, leverage=None,
                margin_usdt=None, close_percent=None, comment=None,
                idempotency_key=None, actor='web-local'):
    store = Store(path)
    try:
        return store.review_plan(
            plan_id, expected_version, decision,
            {'leverage': leverage, 'margin_usdt': margin_usdt,
             'close_percent': close_percent},
            comment, actor, idempotency_key)
    finally:
        store.db.close()
