import json
import sqlite3
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _columns(db, table):
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}


def _add_column(db, table, definition):
    name = definition.split()[0]
    if name not in _columns(db, table):
        db.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')


def _migration_1(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, channel INTEGER NOT NULL, message INTEGER NOT NULL,
        fingerprint TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        analysis TEXT, error TEXT, notified INTEGER NOT NULL DEFAULT 0,
        UNIQUE(channel,message,fingerprint));
    CREATE TABLE IF NOT EXISTS usage (
        id INTEGER PRIMARY KEY, month TEXT NOT NULL, cost REAL NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS agent_attempts (
        id INTEGER PRIMARY KEY, channel INTEGER NOT NULL, message INTEGER NOT NULL,
        step INTEGER NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL,
        validation_errors TEXT);
    CREATE TABLE IF NOT EXISTS analysis_revisions (
        id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, reason TEXT NOT NULL,
        old_status TEXT NOT NULL, old_analysis TEXT, old_error TEXT,
        created_at TEXT NOT NULL);
    ''')


def _payload_metadata(payload, fallback_channel):
    title = f'Канал {fallback_channel}'
    telegram_date = None
    try:
        data = json.loads(payload)
        title = str(data.get('channel_title') or title)[:255]
        messages = data.get('messages') or []
        if messages:
            telegram_date = messages[0].get('date')
    except (TypeError, json.JSONDecodeError):
        pass
    return title, telegram_date


def _migration_2(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER NOT NULL UNIQUE,
        title TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS channel_settings (
        channel_id INTEGER PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
        bank_limit_usdt TEXT,
        sizing_mode TEXT NOT NULL DEFAULT 'percent' CHECK(sizing_mode IN ('percent','fixed_usdt')),
        sizing_value TEXT NOT NULL DEFAULT '5',
        version INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS service_heartbeats (
        service_name TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        pid INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('starting','running','stopping','stopped','failed')),
        details TEXT NOT NULL DEFAULT '{}',
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS control_commands (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','applied','rejected','failed')),
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        applied_at TEXT,
        error TEXT);
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT,
        before_json TEXT,
        after_json TEXT,
        created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_events_status_id ON events(status,id);
    CREATE INDEX IF NOT EXISTS idx_events_channel_message ON events(channel,message);
    CREATE INDEX IF NOT EXISTS idx_control_commands_status_id ON control_commands(status,id);
    CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
    ''')
    _add_column(db, 'events', 'channel_id INTEGER REFERENCES channels(id)')
    _add_column(db, 'events', 'telegram_date TEXT')
    _add_column(db, 'events', 'received_at TEXT')

    now = utc_now()
    rows = db.execute('SELECT id,channel,payload,channel_id,telegram_date FROM events ORDER BY id').fetchall()
    for row in rows:
        title, telegram_date = _payload_metadata(row['payload'], row['channel'])
        db.execute(
            'INSERT INTO channels(telegram_id,title,created_at,updated_at) VALUES (?,?,?,?) '
            'ON CONFLICT(telegram_id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at',
            (row['channel'], title, now, now))
        channel_id = db.execute('SELECT id FROM channels WHERE telegram_id=?', (row['channel'],)).fetchone()[0]
        db.execute(
            'UPDATE events SET channel_id=COALESCE(channel_id,?),telegram_date=COALESCE(telegram_date,?) WHERE id=?',
            (channel_id, telegram_date, row['id']))

    for channel in db.execute('SELECT id FROM channels').fetchall():
        db.execute(
            'INSERT OR IGNORE INTO channel_settings(channel_id,updated_at) VALUES (?,?)',
            (channel['id'], now))
    db.execute(
        "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES ('execution_mode','recognition',?)",
        (now,))
    db.execute(
        "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES ('approval_mode','manual',?)",
        (now,))


def _migration_3(db):
    db.executescript('''
    CREATE INDEX IF NOT EXISTS idx_events_channel_id_id ON events(channel_id,id DESC);
    CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at DESC);
    CREATE INDEX IF NOT EXISTS idx_usage_month ON usage(month);
    CREATE INDEX IF NOT EXISTS idx_analysis_revisions_event ON analysis_revisions(event_id,id DESC);
    ''')


def _migration_4(db):
    _add_column(db, 'channels', "connection_status TEXT NOT NULL DEFAULT 'unknown' "
                "CHECK(connection_status IN ('unknown','ready','error','disabled'))")
    _add_column(db, 'channels', 'last_error TEXT')
    _add_column(db, 'channels', 'checked_at TEXT')
    now = utc_now()
    paused = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()
    paused_value = 'true' if paused and paused[0] == '1' else 'false'
    db.execute(
        "INSERT OR IGNORE INTO app_settings(key,value,updated_at) "
        "VALUES ('recognition_paused',?,?)", (paused_value, now))
    db.execute("UPDATE channels SET connection_status='disabled' WHERE enabled=0")
    db.executescript('''
    CREATE INDEX IF NOT EXISTS idx_channels_enabled ON channels(enabled,id);
    CREATE INDEX IF NOT EXISTS idx_control_commands_created_at ON control_commands(created_at DESC);
    ''')


def _migration_5(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY,
        environment TEXT NOT NULL CHECK(environment IN ('paper','live')),
        channel_id INTEGER NOT NULL REFERENCES channels(id),
        opening_plan_id INTEGER UNIQUE,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('long','short')),
        leverage INTEGER NOT NULL CHECK(leverage > 0),
        isolated INTEGER NOT NULL DEFAULT 1 CHECK(isolated = 1),
        status TEXT NOT NULL CHECK(status IN (
            'pending','open','closing','closed','liquidated','failed')),
        initial_margin_usdt TEXT NOT NULL,
        quantity TEXT,
        remaining_quantity TEXT,
        average_entry_price TEXT,
        stop_price TEXT,
        realized_pnl_usdt TEXT NOT NULL DEFAULT '0',
        unrealized_pnl_usdt TEXT NOT NULL DEFAULT '0',
        opened_at TEXT,
        closed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS trade_plans (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES events(id),
        channel_id INTEGER NOT NULL REFERENCES channels(id),
        signal_index INTEGER NOT NULL,
        analysis_hash TEXT NOT NULL,
        revision INTEGER NOT NULL,
        source_message INTEGER NOT NULL,
        action TEXT NOT NULL,
        symbol TEXT,
        side TEXT,
        order_kind TEXT NOT NULL CHECK(order_kind IN ('market','trigger_limit','unspecified')),
        entry_prices_json TEXT NOT NULL DEFAULT '[]',
        trigger_price TEXT,
        limit_price TEXT,
        effective_leverage INTEGER,
        margin_usdt TEXT,
        bank_limit_snapshot_usdt TEXT,
        sizing_mode TEXT,
        sizing_value TEXT,
        stop_price TEXT,
        take_profits_json TEXT NOT NULL DEFAULT '[]',
        close_percent TEXT,
        related_message_id INTEGER,
        target_position_id INTEGER REFERENCES positions(id),
        isolated INTEGER NOT NULL DEFAULT 1 CHECK(isolated = 1),
        environment TEXT NOT NULL CHECK(environment IN ('recognition','paper','live')),
        status TEXT NOT NULL CHECK(status IN (
            'preview','ready','needs_input','blocked','informational','approved',
            'executing','executed','cancelled','superseded','failed')),
        reason_code TEXT,
        questions_json TEXT NOT NULL DEFAULT '[]',
        evidence TEXT NOT NULL DEFAULT '',
        signal_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(event_id,analysis_hash,signal_index));
    CREATE INDEX IF NOT EXISTS idx_trade_plans_event ON trade_plans(event_id,id);
    CREATE INDEX IF NOT EXISTS idx_trade_plans_status ON trade_plans(status,id);
    CREATE INDEX IF NOT EXISTS idx_trade_plans_symbol ON trade_plans(symbol,status,id);
    CREATE INDEX IF NOT EXISTS idx_trade_plans_channel ON trade_plans(channel_id,status,id);
    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status,id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_one_active_symbol
        ON positions(symbol) WHERE status IN ('pending','open','closing');
    ''')


def _migration_6(db):
    _add_column(db, 'trade_plans', 'version INTEGER NOT NULL DEFAULT 1')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS plan_revisions (
        id INTEGER PRIMARY KEY,
        plan_id INTEGER NOT NULL REFERENCES trade_plans(id),
        old_version INTEGER NOT NULL,
        new_version INTEGER NOT NULL,
        reason TEXT NOT NULL,
        actor TEXT NOT NULL,
        before_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(plan_id,new_version));
    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY,
        plan_id INTEGER NOT NULL REFERENCES trade_plans(id),
        plan_version INTEGER NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
        adjustments_json TEXT NOT NULL DEFAULT '{}',
        comment TEXT,
        actor TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_approvals_plan ON approvals(plan_id,id DESC);
    CREATE INDEX IF NOT EXISTS idx_plan_revisions_plan ON plan_revisions(plan_id,id DESC);
    ''')


def _migration_7(db):
    _add_column(db, 'trade_plans', 'market_check_id INTEGER')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS market_checks (
        id INTEGER PRIMARY KEY,
        plan_id INTEGER NOT NULL REFERENCES trade_plans(id),
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        mexc_symbol TEXT,
        contract_json TEXT NOT NULL DEFAULT '{}',
        ticker_json TEXT NOT NULL DEFAULT '{}',
        candles_json TEXT NOT NULL DEFAULT '{}',
        current_price TEXT,
        bid_price TEXT,
        ask_price TEXT,
        estimated_contracts TEXT,
        estimated_base_quantity TEXT,
        estimated_notional_usdt TEXT,
        observed_high TEXT,
        observed_low TEXT,
        signal_age_seconds INTEGER,
        checked_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_market_checks_plan ON market_checks(plan_id,id DESC);
    CREATE INDEX IF NOT EXISTS idx_market_checks_status ON market_checks(status,checked_at DESC);
    ''')


def _migration_8(db):
    _add_column(db, 'positions', "contract_size TEXT NOT NULL DEFAULT '1'")
    _add_column(db, 'positions', 'allocated_margin_usdt TEXT')
    _add_column(db, 'positions', "take_profits_json TEXT NOT NULL DEFAULT '[]'")
    _add_column(db, 'positions', "entry_fees_usdt TEXT NOT NULL DEFAULT '0'")
    _add_column(db, 'positions', "exit_fees_usdt TEXT NOT NULL DEFAULT '0'")
    _add_column(db, 'positions', 'mark_price TEXT')
    _add_column(db, 'positions', 'last_mark_at TEXT')
    db.execute('''UPDATE positions SET allocated_margin_usdt=initial_margin_usdt
                  WHERE allocated_margin_usdt IS NULL''')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS paper_orders (
        id INTEGER PRIMARY KEY,
        plan_id INTEGER REFERENCES trade_plans(id),
        position_id INTEGER REFERENCES positions(id),
        client_key TEXT NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('long','short')),
        intent TEXT NOT NULL CHECK(intent IN (
            'open','add','close_partial','close_full','set_stop','set_take_profit',
            'stop_loss','take_profit','liquidation')),
        order_kind TEXT NOT NULL CHECK(order_kind IN ('market','trigger_limit')),
        status TEXT NOT NULL CHECK(status IN (
            'pending_trigger','open','filled','cancelled','failed','attention')),
        trigger_price TEXT,
        limit_price TEXT,
        requested_contracts TEXT,
        filled_contracts TEXT NOT NULL DEFAULT '0',
        average_fill_price TEXT,
        reduce_only INTEGER NOT NULL DEFAULT 0 CHECK(reduce_only IN (0,1)),
        error TEXT,
        created_at TEXT NOT NULL,
        triggered_at TEXT,
        filled_at TEXT,
        updated_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_orders_plan
        ON paper_orders(plan_id) WHERE plan_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status,id);
    CREATE INDEX IF NOT EXISTS idx_paper_orders_position ON paper_orders(position_id,id);
    CREATE TABLE IF NOT EXISTS paper_fills (
        id INTEGER PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES paper_orders(id),
        position_id INTEGER NOT NULL REFERENCES positions(id),
        price TEXT NOT NULL,
        contracts TEXT NOT NULL,
        base_quantity TEXT NOT NULL,
        quote_notional_usdt TEXT NOT NULL,
        fee_usdt TEXT NOT NULL,
        realized_pnl_usdt TEXT NOT NULL DEFAULT '0',
        created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_paper_fills_position ON paper_fills(position_id,id);
    CREATE TABLE IF NOT EXISTS paper_notifications (
        id INTEGER PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent')),
        created_at TEXT NOT NULL,
        sent_at TEXT);
    CREATE INDEX IF NOT EXISTS idx_paper_notifications_status
        ON paper_notifications(status,id);
    ''')


def _migration_9(db):
    _add_column(db, 'paper_orders', 'version INTEGER NOT NULL DEFAULT 1')
    _add_column(db, 'paper_orders', 'cancelled_at TEXT')
    _add_column(db, 'paper_orders', 'cancel_reason TEXT')
    _add_column(db, 'positions', "funding_pnl_usdt TEXT NOT NULL DEFAULT '0'")
    _add_column(db, 'positions', 'last_funding_at TEXT')
    _add_column(db, 'positions', 'last_funding_check_at TEXT')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS paper_funding (
        id INTEGER PRIMARY KEY,
        position_id INTEGER NOT NULL REFERENCES positions(id),
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('long','short')),
        rate TEXT NOT NULL,
        position_value_usdt TEXT NOT NULL,
        amount_usdt TEXT NOT NULL,
        settle_time TEXT NOT NULL,
        price_source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(position_id,settle_time));
    CREATE INDEX IF NOT EXISTS idx_paper_funding_position
        ON paper_funding(position_id,settle_time);
    CREATE INDEX IF NOT EXISTS idx_paper_funding_created
        ON paper_funding(created_at DESC);
    ''')


def _migration_10(db):
    _add_column(db, 'positions', "recovery_status TEXT NOT NULL DEFAULT 'ok' "
                "CHECK(recovery_status IN ('ok','attention'))")
    _add_column(db, 'positions', 'recovery_reason TEXT')
    _add_column(db, 'positions', 'recovery_candle_time TEXT')
    _add_column(db, 'positions', 'recovery_high TEXT')
    _add_column(db, 'positions', 'recovery_low TEXT')
    _add_column(db, 'positions', "recovery_options_json TEXT NOT NULL DEFAULT '[]'")
    _add_column(db, 'positions', 'last_recovery_at TEXT')
    _add_column(db, 'paper_orders', "price_source TEXT NOT NULL DEFAULT 'live_bid_ask'")
    _add_column(db, 'paper_orders', 'source_candle_time TEXT')
    _add_column(db, 'paper_fills', "price_source TEXT NOT NULL DEFAULT 'live_bid_ask'")
    _add_column(db, 'paper_fills', 'source_candle_time TEXT')


def _migration_11(db):
    """Private MEXC reconciliation and dormant Live execution state."""
    _add_column(db, 'positions', 'exchange_position_id TEXT')
    _add_column(db, 'positions', 'exchange_state_json TEXT')
    _add_column(db, 'positions', 'last_exchange_sync_at TEXT')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS live_snapshots (
        id INTEGER PRIMARY KEY,
        status TEXT NOT NULL CHECK(status IN ('ready','blocked','error')),
        reason TEXT NOT NULL,
        usdt_available TEXT,
        position_mode TEXT,
        positions_json TEXT NOT NULL DEFAULT '[]',
        orders_json TEXT NOT NULL DEFAULT '[]',
        checked_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_live_snapshots_checked
        ON live_snapshots(checked_at DESC,id DESC);
    CREATE TABLE IF NOT EXISTS live_orders (
        id INTEGER PRIMARY KEY,
        plan_id INTEGER UNIQUE REFERENCES trade_plans(id),
        position_id INTEGER REFERENCES positions(id),
        external_oid TEXT NOT NULL UNIQUE,
        exchange_order_id TEXT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('long','short')),
        intent TEXT NOT NULL CHECK(intent IN (
            'open','add','close_partial','close_full','set_stop','set_take_profit')),
        order_kind TEXT NOT NULL CHECK(order_kind IN ('market','trigger_limit','protection')),
        status TEXT NOT NULL CHECK(status IN (
            'prepared','pending_trigger','submitting','unknown','open','partially_filled',
            'filled','cancel_requested','cancelled','failed','attention')),
        trigger_price TEXT,
        limit_price TEXT,
        requested_contracts TEXT,
        filled_contracts TEXT NOT NULL DEFAULT '0',
        average_fill_price TEXT,
        reduce_only INTEGER NOT NULL DEFAULT 0 CHECK(reduce_only IN (0,1)),
        request_json TEXT,
        response_json TEXT,
        error TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        submitted_at TEXT,
        filled_at TEXT,
        updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_live_orders_status ON live_orders(status,id);
    CREATE INDEX IF NOT EXISTS idx_live_orders_position ON live_orders(position_id,id);
    CREATE TABLE IF NOT EXISTS live_notifications (
        id INTEGER PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent')),
        created_at TEXT NOT NULL,
        sent_at TEXT);
    CREATE INDEX IF NOT EXISTS idx_live_notifications_status
        ON live_notifications(status,id);
    ''')
    now = utc_now()
    db.execute(
        "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES ('live_armed','false',?)",
        (now,))
    db.execute(
        "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES ('live_status','unchecked',?)",
        (now,))


MIGRATIONS = ((1, _migration_1), (2, _migration_2), (3, _migration_3), (4, _migration_4),
              (5, _migration_5), (6, _migration_6), (7, _migration_7),
              (8, _migration_8), (9, _migration_9), (10, _migration_10),
              (11, _migration_11))


def migrate(db: sqlite3.Connection):
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL)''')
    applied = {row[0] for row in db.execute('SELECT version FROM schema_migrations')}
    for version, migration in MIGRATIONS:
        if version in applied:
            continue
        with db:
            migration(db)
            db.execute('INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)',
                       (version, utc_now()))
    return max((version for version, _ in MIGRATIONS), default=0)
