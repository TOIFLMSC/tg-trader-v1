import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import ssl
import sys
import time
import uuid

import httpx
from telethon import TelegramClient, events
from telethon.utils import get_peer_id

from bootstrap import ROOT, SetupError, bot_call, require, settings
from trader.approvals import PlanReviewError
from trader.agent import Agent
from trader.mexc_market import APPROVABLE_STATUSES, MexcMarketClient
from trader.mexc_private import MexcPrivateClient
from trader.live import LiveExecutor, LiveReadiness
from trader.models import Analysis, render
from trader.paper import PaperExecutor
from trader.store import Store


class StopService(Exception):
    pass


@contextmanager
def single_instance():
    path = ROOT / 'data/recognition.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    handle.seek(0)
    if path.stat().st_size == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SetupError('Recognition service is already running.') from None
    try:
        yield
    finally:
        handle.close()


def owner_message(update, owner):
    msg = update.get('message', {})
    return (msg if msg.get('from', {}).get('id') == owner
            and msg.get('chat', {}).get('id') == owner
            and msg.get('chat', {}).get('type') == 'private' else None)


def owner_callback(update, owner):
    callback = update.get('callback_query', {})
    message = callback.get('message', {})
    return (callback if callback.get('from', {}).get('id') == owner
            and message.get('chat', {}).get('id') == owner
            and message.get('chat', {}).get('type') == 'private' else None)


def plan_review_text(plan):
    def value(key, default=None):
        try:
            return plan[key]
        except (KeyError, IndexError):
            return default

    questions = json.loads(plan['questions_json'] or '[]')
    lines = [
        f'ПЛАН #{plan["id"]} · v{plan["version"]} · {plan["status"]}',
        f'{plan["action"]} · {plan["symbol"] or "тикер не определён"} · '
        f'{(plan["side"] or "—").upper()}',
        f'Среда: {plan["environment"]}; isolated',
    ]
    if plan['action'] in ('open', 'add'):
        lines.append(f'Ордер: {plan["order_kind"]}')
        if plan['trigger_price']:
            lines.append(f'Trigger/limit: {plan["trigger_price"]}')
        lines.append(f'Плечо: {plan["effective_leverage"] or "не задано (предлагается 1×)"}')
        lines.append(f'Маржа: {plan["margin_usdt"] or "не задана"} USDT')
    if plan['close_percent']:
        lines.append(f'Закрыть: {plan["close_percent"]}% остатка')
    if plan['reason_code']:
        lines.append(f'Состояние: {plan["reason_code"]}')
    if value('market_status'):
        lines.append(
            f'MEXC: {value("market_status")} · {value("market_reason")}')
        if value('current_price'):
            lines.append(
                f'Цена: {value("current_price")} · bid {value("bid_price") or "—"} · '
                f'ask {value("ask_price") or "—"}')
        if value('estimated_contracts'):
            lines.append(
                f'Расчётный объём: {value("estimated_contracts")} контрактов · '
                f'≈ {value("estimated_notional_usdt")} USDT')
        if value('observed_low') and value('observed_high'):
            lines.append(
                f'После сигнала: low {value("observed_low")} · high {value("observed_high")}')
    elif plan['action'] in ('open', 'add'):
        lines.append('MEXC: публичная проверка ожидается')
    lines.extend(f'Уточнить: {question}' for question in questions[:5])
    if questions or plan['status'] == 'needs_input':
        lines.append(
            f'Ответ текстом: /adjust {plan["id"]} {plan["version"]} | ваш ответ')
        if plan['action'] in ('open', 'add', 'close_partial'):
            lines.append(
                f'Ответ с параметрами: /adjust {plan["id"]} {plan["version"]} '
                'leverage=1 margin=5 close=50 | пояснение')
    lines.append(
        f'Команды: /approve {plan["id"]} {plan["version"]} · '
        f'/skip {plan["id"]} {plan["version"]} · /plan {plan["id"]}')
    if plan['environment'] == 'paper':
        lines.append('После подтверждения действие поступит в виртуальное исполнение.')
    elif plan['environment'] == 'live':
        lines.append('После подтверждения действие поступит в Live только при активной защёлке.')
    else:
        lines.append('Подтверждение сохраняется локально; исполнения нет.')
    return '\n'.join(lines)


def plan_keyboard(plan, manual=True):
    if not manual or plan['status'] not in ('preview', 'ready', 'needs_input', 'blocked'):
        return None
    row = []
    try:
        market_status = plan['market_status']
        has_market_field = True
    except (KeyError, IndexError):
        market_status = None
        has_market_field = False
    # Public MEXC validation is required only for orders which increase a
    # position. Reduce-only and protection actions deliberately have no
    # market_check row, so NULL must not hide their approval button.
    needs_market_check = plan['action'] in ('open', 'add')
    market_ready = (not needs_market_check or
                    (has_market_field and market_status in APPROVABLE_STATUSES))
    if plan['status'] in ('preview', 'ready') and market_ready and market_status != 'stale_review':
        row.append({'text': '✅ Подтвердить',
                    'callback_data': f'plan:{plan["id"]}:{plan["version"]}:approve'})
    row.append({'text': '⏭ Пропустить',
                'callback_data': f'plan:{plan["id"]}:{plan["version"]}:reject'})
    return {'inline_keyboard': [row]}


class Service:
    def __init__(self, cfg, telegram, http, store, instance_id):
        self.cfg, self.telegram, self.http = cfg, telegram, http
        self.store = store
        self.instance_id = instance_id
        self.entities = {}
        self.owner = int(cfg['TELEGRAM_OWNER_ID'])
        self.agent = Agent(cfg, http, store)
        self.market = MexcMarketClient(http) if http is not None else None
        self.paper = PaperExecutor(store, self.market) if self.market is not None else None
        self.private = None
        if http is not None and cfg.get('MEXC_API_KEY') and cfg.get('MEXC_API_SECRET'):
            self.private = MexcPrivateClient(
                http, cfg['MEXC_API_KEY'], cfg['MEXC_API_SECRET'],
                recv_window=int(cfg.get('MEXC_RECV_WINDOW_SECONDS') or 10),
                allow_orders=cfg.get('MEXC_LIVE_ORDERS_ENABLED') == 'YES')
        self.live_readiness = LiveReadiness(store, self.private)
        self.live = (LiveExecutor(store, self.market, self.private)
                     if self.market is not None else None)
        self.ingest_lock = asyncio.Lock()

    async def send(self, text, reply_markup=None):
        # Telegram's limit is UTF-16 based; keep chunks comfortably below it.
        chunks = list(range(0, len(text), 1800)) or [0]
        for index, start in enumerate(chunks):
            payload = {'chat_id': self.owner, 'text': text[start:start + 1800],
                       'link_preview_options': {'is_disabled': True}}
            if reply_markup and index == len(chunks) - 1:
                payload['reply_markup'] = reply_markup
            await bot_call(self.http, self.cfg['TELEGRAM_BOT_TOKEN'], 'sendMessage',
                           payload)

    async def capture(self, message, channel):
        data = {'id': message.id, 'text': message.message or '',
                'date': message.date.isoformat(),
                'edit_date': message.edit_date.isoformat() if message.edit_date else None,
                'reply_to': message.reply_to_msg_id, 'grouped_id': message.grouped_id,
                'author': message.post_author, 'media': []}
        mime = 'image/jpeg' if message.photo else getattr(message.document, 'mime_type', None)
        if message.media and not message.photo and not message.document:
            data['media_note'] = 'Unsupported media type; inspect manually.'
        if message.document and mime not in ('image/jpeg', 'image/png', 'image/webp'):
            data['media_note'] = 'Unsupported attachment; inspect manually.'
        if mime in ('image/jpeg', 'image/png', 'image/webp'):
            if (getattr(message.file, 'size', 0) or 0) > 8_000_000:
                data['media_note'] = 'Image larger than 8 MB; inspect manually.'
            else:
                suffix = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}[mime]
                media_id = (message.photo or message.document).id
                path = ROOT / 'data/media' / str(channel) / f'{message.id}-{media_id}{suffix}'
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    temporary = path.with_suffix('.part')
                    try:
                        await self.telegram.download_media(message, file=str(temporary))
                        if not temporary.exists() or temporary.stat().st_size > 8_000_000:
                            raise SetupError('Media download failed or exceeded size limit')
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
                data['media'].append({'path': str(path.relative_to(ROOT)), 'mime': mime,
                                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return data

    async def ingest(self, messages, channel, entity):
        async with self.ingest_lock:
            messages = sorted((m for m in messages if m and not getattr(m, 'action', None)), key=lambda m: m.id)
            if not messages:
                return
            main = messages[0]
            current = [await self.capture(m, channel) for m in messages]
            parents, seen = [], {m.id for m in messages}
            parent_id = next((m.reply_to_msg_id for m in messages if m.reply_to_msg_id), None)
            for _ in range(3):
                if not parent_id or parent_id in seen:
                    break
                seen.add(parent_id)
                parent = await self.telegram.get_messages(entity, ids=parent_id)
                if not parent:
                    break
                parents.append(await self.capture(parent, channel))
                parent_id = parent.reply_to_msg_id
            payload = {'channel_title': entity.title, 'messages': current, 'parents': parents}
            # Fingerprint depends on the current post, not subsequent edits of its parents.
            # Parent snapshots remain attached to the first reception of this revision.
            current_key = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
            key = f'seen:{channel}:{main.id}:{current_key}'
            if self.store.get(key):
                return
            event_id = self.store.enqueue(channel, main.id, payload)
            self.store.set(key, event_id or 'duplicate')
            if event_id:
                print(f'Queued event {event_id}, post {main.id}', flush=True)

    async def message_handler(self, event):
        channel = int(event.chat_id or 0)
        entity = self.entities.get(channel)
        if entity and not event.message.grouped_id:
            await self.safe_ingest([event.message], channel, entity)

    async def album_handler(self, event):
        channel = int(event.chat_id or 0)
        entity = self.entities.get(channel)
        if entity:
            await self.safe_ingest(event.messages, channel, entity)

    async def edit_handler(self, event):
        channel = int(event.chat_id or 0)
        entity = self.entities.get(channel)
        if not entity:
            return
        if event.message.grouped_id:
            nearby = await self.telegram.get_messages(
                entity, ids=list(range(max(1, event.id - 10), event.id + 11)))
            await self.safe_ingest(
                [m for m in nearby if m and m.grouped_id == event.message.grouped_id],
                channel, entity)
        else:
            await self.safe_ingest([event.message], channel, entity)

    async def safe_ingest(self, messages, channel, entity):
        try:
            await self.ingest(messages, channel, entity)
        except Exception as error:
            print(f'Ingestion failed: {type(error).__name__}', flush=True)
            await self.send('Не удалось сохранить один из постов. Проверьте журнал; сделок не было.')

    async def process_next_event(self):
        if self.store.paused():
            return False
        event = self.store.next_pending()
        if not event:
            return False
        try:
            result, trace = await self.agent.analyze(
                json.loads(event['payload']), event['channel'], event['message'])
            self.store.complete(event['id'], result.model_dump_json())
            self.store.set(f'trace:{event["id"]}', json.dumps(trace))
            print(f'Analyzed event {event["id"]}: {len(result.signals)} actions', flush=True)
        except Exception as error:
            # Do not auto-retry paid or ambiguous requests indefinitely.
            detail = str(error) if isinstance(error, SetupError) else type(error).__name__
            self.store.fail(event['id'], detail)
            print(f'Analysis failed for event {event["id"]}: {type(error).__name__}', flush=True)
        return True

    async def worker(self):
        while True:
            if not await self.process_next_event():
                await asyncio.sleep(1 if self.store.paused() else 0.5)

    async def check_market_plan(self, plan):
        if self.market is None or plan['action'] not in ('open', 'add') or not plan['symbol']:
            return None
        context = self.store.market_plan(plan['id'])
        if not context:
            return None
        values = dict(context)
        event_time = values.get('telegram_date') or values.get('received_at')
        check = await self.market.check_plan(values, event_time)
        self.store.save_market_check(plan['id'], check)
        print(
            f'MEXC check plan {plan["id"]}: {check["status"]} '
            f'{check.get("mexc_symbol") or ""}', flush=True)
        return check

    async def market_worker(self):
        while True:
            plan = self.store.next_market_plan()
            if not plan:
                await asyncio.sleep(2)
                continue
            await self.check_market_plan(plan)
            await asyncio.sleep(0.25)

    async def paper_worker(self):
        while True:
            if self.paper is None or not await self.paper.step():
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(0.15)

    async def paper_notifications(self):
        while True:
            row = self.store.next_paper_notification()
            if not row:
                await asyncio.sleep(0.5)
                continue
            try:
                await self.send(row['text'])
                self.store.paper_notification_sent(row['id'])
            except Exception as error:
                print(f'Paper notification pending: {type(error).__name__}', flush=True)
                await asyncio.sleep(15)

    async def live_worker(self):
        while True:
            if self.live is None or not await self.live.step():
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(0.25)

    async def live_notifications(self):
        while True:
            row = self.store.db.execute('''SELECT * FROM live_notifications
                WHERE status='pending' ORDER BY id LIMIT 1''').fetchone()
            if not row:
                await asyncio.sleep(0.5)
                continue
            try:
                await self.send(row['text'])
                with self.store.db:
                    self.store.db.execute('''UPDATE live_notifications SET status='sent',sent_at=?
                        WHERE id=? AND status='pending' ''',
                        (datetime.now(timezone.utc).isoformat(), row['id']))
            except Exception as error:
                print(f'Live notification pending: {type(error).__name__}', flush=True)
                await asyncio.sleep(15)

    async def deliver(self):
        while True:
            row = self.store.outbox()
            if not row:
                await asyncio.sleep(0.5)
                continue
            try:
                if row['status'] == 'failed':
                    text = (f'Разбор #{row["id"]}, пост #{row["message"]}: ошибка {row["error"]}. '
                            'Сделок нет. Проверим причину локально; автоматический платный повтор отключён.')
                else:
                    payload = json.loads(row['payload'])
                    main = payload['messages'][0]
                    old = (datetime.now(timezone.utc) - datetime.fromisoformat(main['date'])).total_seconds() > 600
                    text = render(Analysis.model_validate_json(row['analysis']), payload['channel_title'],
                                  main['id'], main['reply_to'], bool(main['edit_date']), old)
                    if self.store.get(f'reanalysis:{row["id"]}'):
                        text = 'ИСПРАВЛЕННЫЙ РАЗБОР\n' + text
                    raw_channel = str(abs(int(row['channel'])))
                    if raw_channel.startswith('100'):
                        text += f'\n\nhttps://t.me/c/{raw_channel[3:]}/{main["id"]}'
                await self.send(text)
                if row['status'] == 'done':
                    manual = self.store.app_setting('approval_mode', 'manual') == 'manual'
                    for plan in self.store.plans_for_event(row['id']):
                        if plan['action'] in ('open', 'add') and not plan['market_status']:
                            await self.check_market_plan(plan)
                            plan = self.store.plan(plan['id'])
                        await self.send(plan_review_text(plan), plan_keyboard(plan, manual))
                self.store.notified(row['id'])
            except Exception as error:
                print(f'Notification pending: {type(error).__name__}', flush=True)
                await asyncio.sleep(15)

    def review_command(self, text):
        before_comment, separator, comment = text.partition('|')
        parts = before_comment.split()
        if len(parts) < 3:
            raise PlanReviewError('Укажите ID и версию плана из карточки')
        try:
            plan_id, version = int(parts[1]), int(parts[2])
        except ValueError:
            raise PlanReviewError('ID и версия плана должны быть целыми числами') from None
        adjustments = {}
        aliases = {'leverage': 'leverage', 'margin': 'margin_usdt',
                   'margin_usdt': 'margin_usdt', 'close': 'close_percent',
                   'close_percent': 'close_percent'}
        for token in parts[3:]:
            key, equals, value = token.partition('=')
            if not equals or key not in aliases or not value:
                raise PlanReviewError(f'Неизвестный параметр: {token}')
            adjustments[aliases[key]] = value
        return plan_id, version, adjustments, comment.strip() if separator else None

    async def review_reply(self, plan_id, version, decision, update_id,
                           adjustments=None, comment=None, actor='telegram-owner'):
        result, created = self.store.review_plan(
            plan_id, version, decision, adjustments or {}, comment, actor,
            f'telegram-plan:{update_id}:{decision}')
        plan = self.store.plan(plan_id)
        label = 'подтверждён' if result['decision'] == 'approved' else 'пропущен'
        if not created:
            return f'Решение по плану #{plan_id} уже сохранено.'
        if result['decision'] != 'approved':
            outcome = 'Исполнения не будет.'
        elif plan['environment'] == 'paper':
            outcome = 'Поставлен в очередь виртуального исполнения.'
        elif plan['environment'] == 'live':
            outcome = 'Поставлен в очередь Live; исполнение возможно только при активной защёлке.'
        else:
            outcome = 'Решение сохранено без исполнения.'
        return f'План #{plan_id} {label}; версия {plan["version"]}. {outcome}'

    async def process_bot_update(self, update):
        callback = owner_callback(update, self.owner)
        if callback:
            answer = 'Команда не применена'
            try:
                match = re.fullmatch(r'plan:(\d+):(\d+):(approve|reject)',
                                     callback.get('data', ''))
                if not match:
                    raise PlanReviewError('Некорректная кнопка плана')
                plan_id, version = int(match.group(1)), int(match.group(2))
                answer = await self.review_reply(
                    plan_id, version, match.group(3), update['update_id'],
                    actor='telegram-button')
                await self.send(answer)
            except PlanReviewError as error:
                answer = str(error)
                await self.send('План не изменён: ' + answer)
            finally:
                await bot_call(self.http, self.cfg['TELEGRAM_BOT_TOKEN'],
                               'answerCallbackQuery', {
                                   'callback_query_id': callback['id'], 'text': answer[:180]})
            return

        msg = owner_message(update, self.owner)
        if not msg:
            return
        text = (msg.get('text') or '').strip()
        command = text.split(' ', 1)[0].split('@', 1)[0]
        if command in ('/start', '/help'):
            await self.send(
                'Запущены распознавание и локальное планирование.\n'
                '/status — состояние\n/pending — планы на рассмотрении\n'
                '/plan ID — карточка плана\n/approve ID VERSION — подтвердить\n'
                '/skip ID VERSION — пропустить\n'
                '/adjust ID VERSION leverage=1 margin=5 close=50 | пояснение\n'
                '/pause — приостановить разбор\n/resume — продолжить\n'
                'В paper подтверждение запускает только виртуальное исполнение; '
                'Live требует отдельной локальной проверки и активации в Web UI.')
        elif command in ('/status', '/stats'):
            titles = ', '.join(entity.title for entity in self.entities.values()) or 'нет активных'
            await self.send(f'Режим исполнения: {self.store.app_setting("execution_mode", "recognition")}.\nКаналы: {titles}\n'
                            f'Подтверждения: {self.store.app_setting("approval_mode", "manual")}\n'
                            f'Пауза: {self.store.paused()}\n'
                            f'Разборы: {self.store.stats()}\n'
                            f'Планы: {self.store.trade_stats()}\n'
                            f'LLM за месяц (оценка с резервами): ${self.store.spent():.4f}')
        elif command == '/pending':
            plans = self.store.pending_reviews()
            if not plans:
                await self.send('Планов на рассмотрении нет.')
            for plan in plans:
                await self.send(
                    plan_review_text(plan),
                    plan_keyboard(plan, self.store.app_setting('approval_mode', 'manual') == 'manual'))
        elif command == '/plan':
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit():
                await self.send('Формат: /plan ID')
            else:
                plan = self.store.plan(int(parts[1]))
                if plan:
                    await self.send(plan_review_text(plan), plan_keyboard(plan))
                else:
                    await self.send('План не найден.')
        elif command in ('/approve', '/skip', '/adjust'):
            try:
                plan_id, version, adjustments, comment = self.review_command(text)
                decision = 'reject' if command == '/skip' else 'approve'
                if command != '/adjust' and adjustments:
                    raise PlanReviewError('Параметры разрешены только в /adjust')
                await self.send(await self.review_reply(
                    plan_id, version, decision, update['update_id'], adjustments, comment))
            except PlanReviewError as error:
                await self.send('План не изменён: ' + str(error))
        elif command in ('/pause', '/resume'):
            kind = 'recognition.pause' if command == '/pause' else 'recognition.resume'
            _, created = self.store.queue_control(
                kind, {}, f'telegram:{update["update_id"]}', 'telegram-owner')
            await self.send(
                ('Команда поставлена в очередь. Текущий запрос может завершиться; посты продолжат сохраняться.'
                 if command == '/pause' else 'Команда возобновления поставлена в очередь.')
                if created else 'Эта команда уже принята.')

    async def commands(self):
        offset = int(self.store.get('bot_offset') or 0)
        while True:
            try:
                updates = await bot_call(self.http, self.cfg['TELEGRAM_BOT_TOKEN'], 'getUpdates',
                                         {'offset': offset, 'timeout': 20,
                                          'allowed_updates': ['message', 'callback_query']})
                for update in updates:
                    await self.process_bot_update(update)
                    offset = update['update_id'] + 1
                    self.store.set('bot_offset', offset)
            except Exception as error:
                print(f'Bot polling error: {type(error).__name__}', flush=True)
                await asyncio.sleep(15)

    async def resolve_channel(self, telegram_id):
        entity = await self.telegram.get_entity(int(telegram_id))
        if not getattr(entity, 'broadcast', False):
            raise SetupError('Selected entity is not a broadcast channel')
        actual_id = get_peer_id(entity)
        if actual_id != int(telegram_id):
            raise SetupError('Resolved Telegram channel ID does not match')
        return entity

    async def refresh_channels(self, known=None):
        known = known or {}
        enabled = {int(row['telegram_id']): row for row in self.store.enabled_channels()}
        for telegram_id, row in enabled.items():
            if telegram_id in self.entities:
                continue
            try:
                entity = known.get(telegram_id) or await self.resolve_channel(telegram_id)
                self.entities[telegram_id] = entity
                self.store.set_channel_connection(telegram_id, 'ready')
            except Exception as error:
                self.store.set_channel_connection(
                    telegram_id, 'error', f'{type(error).__name__}: канал недоступен аккаунту reader')
        for telegram_id in list(self.entities):
            if telegram_id not in enabled:
                self.entities.pop(telegram_id, None)

    async def apply_control(self, row):
        payload = json.loads(row['payload'] or '{}')
        kind = row['kind']
        if kind == 'recognition.pause':
            self.store.set_paused(True)
        elif kind == 'recognition.resume':
            self.store.set_paused(False)
        elif kind == 'approval.set':
            mode = payload.get('mode')
            if mode not in ('manual', 'auto'):
                raise ValueError('Недопустимый режим подтверждений')
            if mode == 'auto' and self.store.app_setting('live_armed', 'false') == 'true':
                raise ValueError('Auto approvals для Live ещё не разрешены; снимите Live-защёлку')
            self.store.set_app_setting('approval_mode', mode)
        elif kind == 'execution.set':
            mode = payload.get('mode')
            if mode not in ('recognition', 'paper', 'live'):
                raise ValueError('Недопустимый режим исполнения')
            if mode == 'live':
                if self.store.app_setting('live_armed', 'false') != 'true':
                    raise ValueError('live не активирован отдельным подтверждением')
                snapshot = self.store.db.execute(
                    'SELECT * FROM live_snapshots ORDER BY id DESC LIMIT 1').fetchone()
                if not snapshot or snapshot['status'] != 'ready':
                    raise ValueError('Нет успешной проверки приватного MEXC API')
                checked = datetime.fromisoformat(snapshot['checked_at'].replace('Z', '+00:00'))
                if (datetime.now(timezone.utc) - checked).total_seconds() > 300:
                    raise ValueError('Проверка MEXC устарела; выполните её заново')
                if not self.private or not self.private.allow_orders:
                    raise ValueError('Live-записи отключены в локальной конфигурации')
            elif self.store.app_setting('execution_mode', 'recognition') == 'live':
                self.store.set_app_setting('live_armed', 'false')
            self.store.set_app_setting('execution_mode', mode)
        elif kind == 'live.check':
            snapshot_id = await self.live_readiness.check()
            print(f'Live readiness snapshot {snapshot_id} saved', flush=True)
        elif kind == 'live.activate':
            snapshot = self.store.db.execute(
                'SELECT * FROM live_snapshots ORDER BY id DESC LIMIT 1').fetchone()
            if not snapshot or snapshot['status'] != 'ready':
                raise ValueError('Сначала нужна успешная проверка MEXC')
            confirmation = str(payload.get('confirmation') or '').strip()
            if confirmation != f'LIVE {snapshot["id"]}':
                raise ValueError(f'Введите точную фразу LIVE {snapshot["id"]}')
            checked = datetime.fromisoformat(snapshot['checked_at'].replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - checked).total_seconds() > 300:
                raise ValueError('Проверка MEXC устарела; выполните её заново')
            if self.store.app_setting('approval_mode', 'manual') != 'manual':
                raise ValueError('Первая активация Live разрешена только в ручном режиме')
            if not self.private or not self.private.allow_orders:
                raise ValueError('Установите MEXC_LIVE_ORDERS_ENABLED=YES и перезапустите reader')
            self.store.set_app_setting('live_armed', 'true')
        elif kind == 'live.disarm':
            self.store.set_app_setting('live_armed', 'false')
            if self.store.app_setting('execution_mode', 'recognition') == 'live':
                self.store.set_app_setting('execution_mode', 'recognition')
        elif kind == 'channel.add':
            try:
                telegram_id = int(payload.get('telegram_id'))
            except (TypeError, ValueError):
                raise ValueError('Telegram ID канала должен быть целым числом') from None
            if not str(telegram_id).startswith('-100'):
                raise ValueError('Telegram ID канала должен начинаться с -100')
            entity = await self.resolve_channel(telegram_id)
            channel_id = self.store.ensure_channel(telegram_id, entity.title)
            self.store.set_channel_enabled(channel_id, True)
            self.entities[telegram_id] = entity
            self.store.set_channel_connection(telegram_id, 'ready')
        elif kind == 'channel.monitor':
            try:
                channel_id = int(payload.get('channel_id'))
            except (TypeError, ValueError):
                raise ValueError('Некорректный ID канала') from None
            enabled = payload.get('enabled') is True
            channel = self.store.channel(channel_id)
            if not channel:
                raise ValueError('Канал не найден')
            telegram_id = int(channel['telegram_id'])
            if enabled:
                entity = await self.resolve_channel(telegram_id)
                self.store.set_channel_enabled(channel_id, True)
                self.entities[telegram_id] = entity
                self.store.set_channel_connection(telegram_id, 'ready')
            else:
                self.store.set_channel_enabled(channel_id, False)
                self.entities.pop(telegram_id, None)
        elif kind == 'paper_order.cancel':
            try:
                order_id = int(payload.get('order_id'))
                version = int(payload.get('version'))
            except (TypeError, ValueError):
                raise ValueError('Некорректный ID или версия paper-заявки') from None
            self.store.cancel_paper_order(order_id, version)
        elif kind == 'paper_position.resume_recovery':
            try:
                position_id = int(payload.get('position_id'))
                version = int(payload.get('version'))
            except (TypeError, ValueError):
                raise ValueError('Некорректный ID или версия paper-позиции') from None
            self.store.resume_paper_position(position_id, version)
        elif kind == 'live_order.cancel':
            try:
                order_id = int(payload.get('order_id'))
                version = int(payload.get('version'))
            except (TypeError, ValueError):
                raise ValueError('Некорректный ID или версия Live-заявки') from None
            if self.live is None:
                raise ValueError('Live executor недоступен')
            await self.live.cancel_order(order_id, version)
        else:
            raise ValueError('Неизвестная управляющая команда')

    async def process_next_control(self):
        row = self.store.next_control()
        if not row:
            return False
        try:
            await self.apply_control(row)
            self.store.finish_control(row['id'], 'applied')
            print(f'Applied control command {row["id"]}: {row["kind"]}', flush=True)
        except ValueError as error:
            self.store.finish_control(row['id'], 'rejected', str(error))
            print(f'Rejected control command {row["id"]}: {error}', flush=True)
        except Exception as error:
            self.store.finish_control(
                row['id'], 'failed', f'{type(error).__name__}: команда не применена')
            print(f'Control command {row["id"]} failed: {type(error).__name__}', flush=True)
        return True

    async def control(self):
        while True:
            if not await self.process_next_control():
                await asyncio.sleep(0.5)

    async def lifecycle(self):
        next_heartbeat = 0.0
        next_refresh = 0.0
        while True:
            if time.monotonic() >= next_refresh:
                await self.refresh_channels()
                next_refresh = time.monotonic() + 60
            if time.monotonic() >= next_heartbeat:
                self.store.heartbeat('recognition', self.instance_id, 'running', {
                    'channel_ids': sorted(self.entities),
                    'channel_titles': sorted(entity.title for entity in self.entities.values()),
                    'paused': self.store.paused(),
                    'execution_mode': self.store.app_setting('execution_mode', 'recognition'),
                    'approval_mode': self.store.app_setting('approval_mode', 'manual'),
                })
                next_heartbeat = time.monotonic() + 5
            if (ROOT / 'data/recognition.stop').exists():
                raise StopService()
            if self.telegram.disconnected.done():
                raise SetupError('Telegram disconnected. Restart recognition after checking connectivity.')
            await asyncio.sleep(1)


async def run(check=False):
    cfg = settings()
    require(cfg, 'TELEGRAM_OWNER_ID', 'TELEGRAM_TEST_CHANNEL_ID', 'TELEGRAM_API_ID',
            'TELEGRAM_API_HASH', 'TELEGRAM_PHONE', 'TELEGRAM_BOT_TOKEN', 'OPENAI_API_KEY', 'OPENAI_MODEL')
    if int(cfg['TELEGRAM_OWNER_ID']) <= 0 or int(cfg['TELEGRAM_TEST_CHANNEL_ID']) >= 0:
        raise SetupError('Invalid owner or channel ID')
    session = (ROOT / cfg.get('TELEGRAM_SESSION_PATH', 'data/telegram/reader.session')).resolve()
    if not session.is_relative_to(ROOT / 'data') or not session.exists():
        raise SetupError('Run setup-local.cmd first')
    telegram = TelegramClient(str(session), int(cfg['TELEGRAM_API_ID']), cfg['TELEGRAM_API_HASH'])
    try:
        await telegram.connect()
        if not await telegram.is_user_authorized():
            raise SetupError('Telegram session expired. Run setup-local.cmd.')
        me = await telegram.get_me()
        if me.phone != cfg['TELEGRAM_PHONE'].lstrip('+') or me.bot:
            raise SetupError('Reader session does not match configured phone')
        entity = await telegram.get_entity(int(cfg['TELEGRAM_TEST_CHANNEL_ID']))
        if not getattr(entity, 'broadcast', False):
            raise SetupError('Selected entity is not a channel')
        async with httpx.AsyncClient(verify=ssl.create_default_context(), timeout=90, follow_redirects=False) as http:
            bot = await bot_call(http, cfg['TELEGRAM_BOT_TOKEN'], 'getMe')
            webhook = await bot_call(http, cfg['TELEGRAM_BOT_TOKEN'], 'getWebhookInfo')
            if webhook.get('url'):
                raise SetupError('Bot already has a webhook; it was not changed')
            await bot_call(http, cfg['TELEGRAM_BOT_TOKEN'], 'getChat', {'chat_id': int(cfg['TELEGRAM_OWNER_ID'])})
            print(f'Reader authorized; initial channel accessible; owner chat accessible; bot @{bot["username"]}', flush=True)
            if check:
                return
            (ROOT / 'data/recognition.stop').unlink(missing_ok=True)
            store = Store(ROOT / 'data/recognition.sqlite3')
            instance_id = uuid.uuid4().hex
            store.ensure_channel(get_peer_id(entity), entity.title)
            store.heartbeat('recognition', instance_id, 'starting', {
                'channel_id': get_peer_id(entity), 'channel_title': entity.title,
            })
            service = Service(cfg, telegram, http, store, instance_id)
            await service.refresh_channels({get_peer_id(entity): entity})
            telegram.add_event_handler(service.message_handler, events.NewMessage())
            telegram.add_event_handler(service.album_handler, events.Album())
            telegram.add_event_handler(service.edit_handler, events.MessageEdited())
            execution_mode = store.app_setting('execution_mode', 'recognition')
            await service.send(
                'Подключение готово. Читаю новые посты включённых каналов и присылаю '
                'разбор текста/изображений. '
                f'Текущий режим исполнения: {execution_mode}. '
                'В recognition создаются только планы; в paper исполняются только локальные '
                'виртуальные заявки по публичным котировкам MEXC; Live защищён отдельной '
                'приватной проверкой и защёлкой в Web UI. '
                'Опубликуй новый тестовый сигнал. Команды: /status, /stats, /pause, /resume.')
            print(f'READY: listening for {len(service.entities)} enabled channel(s). Ctrl+C to stop.', flush=True)
            final_status = 'failed'
            try:
                try:
                    async with asyncio.TaskGroup() as group:
                        group.create_task(service.worker())
                        group.create_task(service.market_worker())
                        group.create_task(service.paper_worker())
                        group.create_task(service.paper_notifications())
                        group.create_task(service.live_worker())
                        group.create_task(service.live_notifications())
                        group.create_task(service.deliver())
                        group.create_task(service.commands())
                        group.create_task(service.control())
                        group.create_task(service.lifecycle())
                except* StopService:
                    print('Stopped by local request.', flush=True)
                final_status = 'stopped'
            finally:
                store.heartbeat('recognition', instance_id, final_status, {
                    'channel_ids': sorted(service.entities),
                    'channel_titles': sorted(item.title for item in service.entities.values()),
                })
                store.db.close()
    finally:
        await telegram.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Verify bindings without starting recognition or sending messages.')
    parser.add_argument('--stop', action='store_true', help='Stop the local recognition process gracefully.')
    args = parser.parse_args()
    if args.stop:
        (ROOT / 'data').mkdir(exist_ok=True)
        (ROOT / 'data/recognition.stop').touch()
        print('Local stop requested.')
        return 0
    try:
        with single_instance():
            pid_path = ROOT / 'data/recognition.pid'
            pid_path.write_text(str(os.getpid()), encoding='ascii')
            try:
                asyncio.run(run(args.check))
            finally:
                try:
                    if pid_path.read_text(encoding='ascii').strip() == str(os.getpid()):
                        pid_path.unlink(missing_ok=True)
                except (FileNotFoundError, OSError):
                    pass
        return 0
    except KeyboardInterrupt:
        print('Stopped.', flush=True)
        return 0
    except SetupError as error:
        print(f'ERROR: {error}', flush=True)
    except Exception as error:
        print(f'ERROR: {type(error).__name__}. Secret details hidden.', flush=True)
    return 1


if __name__ == '__main__':
    sys.exit(main())
