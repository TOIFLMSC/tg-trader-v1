import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
import hmac
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

from bootstrap import ROOT
from trader.approvals import PlanReviewError
from trader.migrations import migrate, utc_now
from trader.webapp.repository import (
    SettingsConflict,
    SettingsError,
    connect,
    control_data,
    dashboard_data,
    enqueue_control,
    event_detail,
    format_timestamp,
    list_channels,
    list_events,
    media_file,
    recognition_stats,
    review_plan,
    trade_data,
    update_channel_settings,
)


ASSETS = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'data/recognition.sqlite3'
LOOPBACK_HOSTS = frozenset(('127.0.0.1', 'localhost'))


def valid_form_origin(request, origin):
    """Accept browser form posts that still carry a valid double-submit token.

    Some embedded browsers omit Origin on a same-origin navigation.  An explicit
    non-local Origin is still rejected.  Loopback aliases are equivalent because
    the server is bound only to 127.0.0.1.
    """
    if not origin or origin == 'null':
        return True
    try:
        supplied = urlsplit(origin)
    except ValueError:
        return False
    if supplied.scheme != request.url.scheme:
        return False
    expected_port = request.url.port or (443 if request.url.scheme == 'https' else 80)
    supplied_port = supplied.port or (443 if supplied.scheme == 'https' else 80)
    if supplied_port != expected_port:
        return False
    expected_host = (request.url.hostname or '').lower()
    supplied_host = (supplied.hostname or '').lower()
    return (supplied_host == expected_host
            or supplied_host in LOOPBACK_HOSTS and expected_host in LOOPBACK_HOSTS)


@contextmanager
def single_instance():
    path = ROOT / 'data/ui.lock'
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
        raise RuntimeError('TG Trader UI is already running.') from None
    try:
        yield
    finally:
        handle.close()


def write_heartbeat(path, instance_id, status, started_at):
    now = utc_now()
    db = connect(path)
    try:
        with db:
            db.execute('''
                INSERT INTO service_heartbeats(
                    service_name,instance_id,pid,status,details,started_at,updated_at)
                VALUES ('web',?,?,?,?,?,?)
                ON CONFLICT(service_name) DO UPDATE SET
                    instance_id=excluded.instance_id,pid=excluded.pid,status=excluded.status,
                    details=excluded.details,
                    started_at=CASE WHEN service_heartbeats.instance_id=excluded.instance_id
                        THEN service_heartbeats.started_at ELSE excluded.started_at END,
                    updated_at=excluded.updated_at
                ''', (instance_id, os.getpid(), status, '{}', started_at, now))
    finally:
        db.close()


def create_app(db_path=None):
    database = Path(db_path or DEFAULT_DB)
    database.parent.mkdir(parents=True, exist_ok=True)
    db = connect(database)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        migrate(db)
        db.commit()
    finally:
        db.close()

    instance_id = uuid.uuid4().hex
    started_at = utc_now()

    async def heartbeat_loop():
        while True:
            write_heartbeat(database, instance_id, 'running', started_at)
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(application):
        pid_path = ROOT / 'data/ui.pid'
        pid_path.write_text(str(os.getpid()), encoding='ascii')
        write_heartbeat(database, instance_id, 'starting', started_at)
        task = asyncio.create_task(heartbeat_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            write_heartbeat(database, instance_id, 'stopped', started_at)
            try:
                if pid_path.read_text(encoding='ascii').strip() == str(os.getpid()):
                    pid_path.unlink(missing_ok=True)
            except (FileNotFoundError, OSError):
                pass

    web = FastAPI(title='TG Trader Control', docs_url=None, redoc_url=None, lifespan=lifespan)
    web.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
    web.state.db_path = database
    web.mount('/static', StaticFiles(directory=ASSETS / 'static'), name='static')
    templates = Jinja2Templates(directory=ASSETS / 'templates')
    templates.env.filters['datetime'] = format_timestamp

    @web.middleware('http')
    async def csrf_cookie(request: Request, call_next):
        token = request.cookies.get('tgtrader_csrf') or secrets.token_urlsafe(32)
        request.state.csrf = token
        response = await call_next(request)
        if 'tgtrader_csrf' not in request.cookies:
            response.set_cookie('tgtrader_csrf', token, httponly=True, samesite='strict', path='/')
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        return response

    def render(request, name, context=None, status_code=200):
        values = {'csrf': request.state.csrf}
        values.update(context or {})
        return templates.TemplateResponse(
            request=request, name=name, context=values, status_code=status_code)

    async def verified_form(request):
        content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        if content_type != 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'Expected URL-encoded form')
        body = await request.body()
        if len(body) > 16_384:
            raise HTTPException(413, 'Form is too large')
        try:
            values = {key: items[-1] for key, items in parse_qs(
                body.decode('utf-8'), keep_blank_values=True, max_num_fields=20).items()}
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(400, 'Invalid form') from None
        supplied = values.get('csrf', '')
        expected = request.cookies.get('tgtrader_csrf', '')
        if not supplied or not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(403, 'CSRF check failed')
        origin = request.headers.get('origin')
        if not valid_form_origin(request, origin):
            raise HTTPException(403, 'Origin check failed')
        return values

    @web.get('/', response_class=HTMLResponse)
    async def dashboard(request: Request):
        return render(request, 'dashboard.html', {
            'active_page': 'dashboard', 'data': dashboard_data(web.state.db_path)})

    @web.get('/partials/overview', response_class=HTMLResponse)
    async def dashboard_partial(request: Request):
        return render(request, 'partials/dashboard_live.html', {
            'data': dashboard_data(web.state.db_path)})

    def channels_response(request, saved=False, queued=False, error=None, status_code=200):
        channels = list_channels(web.state.db_path)
        for channel in channels:
            channel['command_token'] = secrets.token_urlsafe(24)
        return render(request, 'channels.html', {
            'active_page': 'channels', 'channels': channels,
            'saved': saved, 'queued': queued, 'error': error,
            'add_token': secrets.token_urlsafe(24),
        }, status_code)

    @web.get('/channels', response_class=HTMLResponse)
    async def channels_page(request: Request, saved: int = 0, queued: int = 0):
        return channels_response(request, bool(saved), bool(queued))

    @web.post('/channels/{channel_id}', response_class=HTMLResponse)
    async def save_channel(request: Request, channel_id: int):
        values = await verified_form(request)
        try:
            update_channel_settings(
                web.state.db_path, channel_id,
                values.get('bank_limit_usdt'), values.get('sizing_mode'),
                values.get('sizing_value'), values.get('version'))
        except SettingsConflict as error:
            return channels_response(request, error=str(error), status_code=409)
        except SettingsError as error:
            return channels_response(request, error=str(error), status_code=422)
        return RedirectResponse('/channels?saved=1', status_code=303)

    @web.post('/channel-actions/add', response_class=HTMLResponse)
    async def add_channel(request: Request):
        values = await verified_form(request)
        raw = (values.get('telegram_id') or '').strip()
        if not raw.startswith('-100') or not raw[1:].isdigit():
            return channels_response(
                request, error='Укажите Telegram ID канала в формате -100…', status_code=422)
        try:
            enqueue_control(web.state.db_path, 'channel.add', {'telegram_id': int(raw)},
                            values.get('idempotency_key'))
        except SettingsError as error:
            return channels_response(request, error=str(error), status_code=422)
        return RedirectResponse('/channels?queued=1', status_code=303)

    @web.post('/channels/{channel_id}/monitoring', response_class=HTMLResponse)
    async def channel_monitoring(request: Request, channel_id: int):
        values = await verified_form(request)
        if values.get('enabled') not in ('0', '1'):
            raise HTTPException(422, 'Invalid monitoring state')
        try:
            enqueue_control(web.state.db_path, 'channel.monitor', {
                'channel_id': channel_id, 'enabled': values['enabled'] == '1'},
                values.get('idempotency_key'))
        except SettingsError as error:
            return channels_response(request, error=str(error), status_code=422)
        return RedirectResponse('/channels?queued=1', status_code=303)

    def control_context():
        return {'control': control_data(web.state.db_path),
                'recognition_token': secrets.token_urlsafe(24),
                'approval_token': secrets.token_urlsafe(24),
                'execution_token': secrets.token_urlsafe(24),
                'live_check_token': secrets.token_urlsafe(24),
                'live_activate_token': secrets.token_urlsafe(24),
                'live_disarm_token': secrets.token_urlsafe(24)}

    @web.get('/control', response_class=HTMLResponse)
    async def control_page(request: Request, queued: int = 0):
        context = control_context()
        context.update(active_page='control', queued=bool(queued))
        return render(request, 'control.html', context)

    @web.get('/partials/control', response_class=HTMLResponse)
    async def control_partial(request: Request):
        return render(request, 'partials/control_live.html', control_context())

    @web.post('/control/recognition', response_class=HTMLResponse)
    async def control_recognition(request: Request):
        values = await verified_form(request)
        action = values.get('action')
        if action not in ('pause', 'resume'):
            raise HTTPException(422, 'Invalid recognition action')
        try:
            enqueue_control(web.state.db_path, f'recognition.{action}', {},
                            values.get('idempotency_key'))
        except SettingsError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse('/control?queued=1', status_code=303)

    @web.post('/control/approval', response_class=HTMLResponse)
    async def control_approval(request: Request):
        values = await verified_form(request)
        mode = values.get('mode')
        if mode not in ('manual', 'auto'):
            raise HTTPException(422, 'Invalid approval mode')
        try:
            enqueue_control(web.state.db_path, 'approval.set', {'mode': mode},
                            values.get('idempotency_key'))
        except SettingsError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse('/control?queued=1', status_code=303)

    @web.post('/control/execution', response_class=HTMLResponse)
    async def control_execution(request: Request):
        values = await verified_form(request)
        mode = values.get('mode')
        if mode not in ('recognition', 'paper', 'live'):
            raise HTTPException(422, 'Invalid execution mode')
        try:
            enqueue_control(web.state.db_path, 'execution.set', {'mode': mode},
                            values.get('idempotency_key'))
        except SettingsError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse('/control?queued=1', status_code=303)

    @web.post('/control/live/check', response_class=HTMLResponse)
    async def control_live_check(request: Request):
        values = await verified_form(request)
        try:
            enqueue_control(web.state.db_path, 'live.check', {},
                            values.get('idempotency_key'))
        except SettingsError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse('/control?queued=1', status_code=303)

    @web.post('/control/live/activate', response_class=HTMLResponse)
    async def control_live_activate(request: Request):
        values = await verified_form(request)
        confirmation = (values.get('confirmation') or '').strip()
        if not confirmation.startswith('LIVE '):
            raise HTTPException(422, 'Введите фразу подтверждения из карточки')
        try:
            enqueue_control(web.state.db_path, 'live.activate', {
                'confirmation': confirmation}, values.get('idempotency_key'))
        except SettingsError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse('/control?queued=1', status_code=303)

    @web.post('/control/live/disarm', response_class=HTMLResponse)
    async def control_live_disarm(request: Request):
        values = await verified_form(request)
        try:
            enqueue_control(web.state.db_path, 'live.disarm', {},
                            values.get('idempotency_key'))
        except SettingsError as error:
            raise HTTPException(422, str(error)) from None
        return RedirectResponse('/control?queued=1', status_code=303)

    def post_filters(channel, status, action, symbol, page):
        return {'channel': channel, 'status': status or '', 'action': action or '',
                'symbol': symbol or '', 'page': page}

    def posts_context(channel, status, action, symbol, page):
        result = list_events(web.state.db_path, channel, status, action, symbol, page)
        filters = post_filters(channel, status, action, symbol, result['page'])
        query = urlencode({key: value for key, value in filters.items() if value not in (None, '')})
        def page_url(number):
            values = dict(filters)
            values['page'] = number
            return '/posts?' + urlencode({key: value for key, value in values.items()
                                          if value not in (None, '')})
        return {'result': result, 'filters': filters, 'channels': list_channels(web.state.db_path),
                'partial_url': '/partials/posts' + (f'?{query}' if query else ''),
                'prev_url': page_url(result['page'] - 1) if result['page'] > 1 else None,
                'next_url': page_url(result['page'] + 1) if result['page'] < result['pages'] else None}

    @web.get('/posts', response_class=HTMLResponse)
    async def posts_page(request: Request, channel: int | None = None, status: str = '',
                         action: str = '', symbol: str = '', page: int = 1):
        context = posts_context(channel, status, action, symbol, page)
        context['active_page'] = 'posts'
        return render(request, 'posts.html', context)

    @web.get('/partials/posts', response_class=HTMLResponse)
    async def posts_partial(request: Request, channel: int | None = None, status: str = '',
                            action: str = '', symbol: str = '', page: int = 1):
        return render(request, 'partials/posts_list.html',
                      posts_context(channel, status, action, symbol, page))

    @web.get('/posts/{event_id}', response_class=HTMLResponse)
    async def post_detail(request: Request, event_id: int):
        event = event_detail(web.state.db_path, event_id)
        if not event:
            raise HTTPException(404, 'Event not found')
        return render(request, 'post_detail.html', {
            'active_page': 'posts', 'event': event})

    @web.get('/media/{event_id}/{index}')
    async def event_media(event_id: int, index: int):
        found = media_file(web.state.db_path, event_id, index)
        if not found:
            raise HTTPException(404, 'Media not found')
        path, mime = found
        return FileResponse(path, media_type=mime, headers={
            'Cache-Control': 'private, max-age=3600',
            'X-Content-Type-Options': 'nosniff',
        })

    def stats_context(equity_period):
        stats = recognition_stats(web.state.db_path, equity_period)
        period = stats['equity_curve']['period']
        return {
            'stats': stats,
            'stats_partial_url': '/partials/stats?' + urlencode({'equity_period': period}),
        }

    @web.get('/stats', response_class=HTMLResponse)
    async def stats_page(request: Request, equity_period: str = 'days'):
        return render(request, 'stats.html', {
            'active_page': 'stats', **stats_context(equity_period)})

    @web.get('/partials/stats', response_class=HTMLResponse)
    async def stats_partial(request: Request, equity_period: str = 'days'):
        return render(request, 'partials/stats_live.html', stats_context(equity_period))

    def trades_context(positions_page=1, orders_page=1, plans_page=1):
        data = trade_data(
            web.state.db_path, positions_page, orders_page, plans_page, per_page=10)
        for plan in data['plans']:
            plan['review_token'] = secrets.token_urlsafe(24)
        for position in data['positions']:
            position['recovery_token'] = secrets.token_urlsafe(24)
        for order in data['orders']:
            order['cancel_token'] = secrets.token_urlsafe(24)
        for order in data['live_orders']:
            order['cancel_token'] = secrets.token_urlsafe(24)
        pages = {
            'positions_page': data['positions_pagination']['page'],
            'orders_page': data['orders_pagination']['page'],
            'plans_page': data['plans_pagination']['page'],
        }
        data['page_query'] = urlencode(pages)
        data['partial_url'] = '/partials/trades?' + data['page_query']
        for name in ('positions', 'orders', 'plans'):
            pagination = data[f'{name}_pagination']
            page_key = f'{name}_page'
            if pagination['page'] > 1:
                values = dict(pages)
                values[page_key] = pagination['page'] - 1
                pagination['prev_url'] = '/trades?' + urlencode(values)
            else:
                pagination['prev_url'] = None
            if pagination['page'] < pagination['pages']:
                values = dict(pages)
                values[page_key] = pagination['page'] + 1
                pagination['next_url'] = '/trades?' + urlencode(values)
            else:
                pagination['next_url'] = None
        return {'trades': data}

    def trades_response(request, reviewed=False, queued=False, error=None, status_code=200,
                        positions_page=1, orders_page=1, plans_page=1):
        context = trades_context(positions_page, orders_page, plans_page)
        context.update(
            active_page='trades', reviewed=reviewed, queued=queued, error=error)
        return render(request, 'trades.html', {
            **context}, status_code)

    @web.get('/trades', response_class=HTMLResponse)
    async def trades_page(request: Request, reviewed: int = 0, queued: int = 0,
                          positions_page: int = 1, orders_page: int = 1,
                          plans_page: int = 1):
        return trades_response(
            request, bool(reviewed), bool(queued), positions_page=positions_page,
            orders_page=orders_page, plans_page=plans_page)

    @web.get('/partials/trades', response_class=HTMLResponse)
    async def trades_partial(request: Request, positions_page: int = 1,
                             orders_page: int = 1, plans_page: int = 1):
        return render(request, 'partials/trades_live.html', trades_context(
            positions_page, orders_page, plans_page))

    @web.post('/plans/{plan_id}/review', response_class=HTMLResponse)
    async def plan_review(request: Request, plan_id: int, positions_page: int = 1,
                          orders_page: int = 1, plans_page: int = 1):
        values = await verified_form(request)
        try:
            expected_version = int(values.get('expected_version') or '')
            review_plan(
                web.state.db_path, plan_id, expected_version, values.get('decision'),
                values.get('leverage'), values.get('margin_usdt'),
                values.get('close_percent'), values.get('comment'),
                values.get('idempotency_key'))
        except (TypeError, ValueError, PlanReviewError) as error:
            status = 409 if 'изменён' in str(error) else 422
            return trades_response(
                request, error=str(error), status_code=status,
                positions_page=positions_page, orders_page=orders_page,
                plans_page=plans_page)
        query = urlencode({
            'reviewed': 1, 'positions_page': positions_page,
            'orders_page': orders_page, 'plans_page': plans_page,
        })
        return RedirectResponse('/trades?' + query, status_code=303)

    @web.post('/paper-orders/{order_id}/cancel', response_class=HTMLResponse)
    async def paper_order_cancel(request: Request, order_id: int, positions_page: int = 1,
                                 orders_page: int = 1, plans_page: int = 1):
        values = await verified_form(request)
        try:
            version = int(values.get('expected_version') or '')
            if version <= 0:
                raise ValueError('Некорректная версия')
            enqueue_control(web.state.db_path, 'paper_order.cancel', {
                'order_id': order_id, 'version': version,
            }, values.get('idempotency_key'))
        except (TypeError, ValueError, SettingsError) as error:
            return trades_response(
                request, error=str(error), status_code=422,
                positions_page=positions_page, orders_page=orders_page,
                plans_page=plans_page)
        query = urlencode({
            'queued': 1, 'positions_page': positions_page,
            'orders_page': orders_page, 'plans_page': plans_page,
        })
        return RedirectResponse('/trades?' + query, status_code=303)

    @web.post('/live-orders/{order_id}/cancel', response_class=HTMLResponse)
    async def live_order_cancel(request: Request, order_id: int, positions_page: int = 1,
                                orders_page: int = 1, plans_page: int = 1):
        values = await verified_form(request)
        try:
            version = int(values.get('expected_version') or '')
            if version <= 0:
                raise ValueError('Некорректная версия')
            enqueue_control(web.state.db_path, 'live_order.cancel', {
                'order_id': order_id, 'version': version,
            }, values.get('idempotency_key'))
        except (TypeError, ValueError, SettingsError) as error:
            return trades_response(
                request, error=str(error), status_code=422,
                positions_page=positions_page, orders_page=orders_page,
                plans_page=plans_page)
        query = urlencode({
            'queued': 1, 'positions_page': positions_page,
            'orders_page': orders_page, 'plans_page': plans_page,
        })
        return RedirectResponse('/trades?' + query, status_code=303)

    @web.post('/positions/{position_id}/recovery/resume', response_class=HTMLResponse)
    async def paper_position_recovery_resume(
            request: Request, position_id: int, positions_page: int = 1,
            orders_page: int = 1, plans_page: int = 1):
        values = await verified_form(request)
        try:
            version = int(values.get('expected_version') or '')
            if version <= 0:
                raise ValueError('Некорректная версия')
            enqueue_control(web.state.db_path, 'paper_position.resume_recovery', {
                'position_id': position_id, 'version': version,
            }, values.get('idempotency_key'))
        except (TypeError, ValueError, SettingsError) as error:
            return trades_response(
                request, error=str(error), status_code=422,
                positions_page=positions_page, orders_page=orders_page,
                plans_page=plans_page)
        query = urlencode({
            'queued': 1, 'positions_page': positions_page,
            'orders_page': orders_page, 'plans_page': plans_page,
        })
        return RedirectResponse('/trades?' + query, status_code=303)

    @web.get('/healthz')
    async def health():
        data = dashboard_data(web.state.db_path)
        return {'status': 'ok', 'schema_version': data['schema_version'],
                'channels': len(data['channels']), 'events': sum(data['counts'].values())}

    return web


def main():
    cfg = dotenv_values(ROOT / '.env', encoding='utf-8-sig', interpolate=False)
    port = int(cfg.get('UI_PORT') or 8787)
    if not 1024 <= port <= 65535:
        raise SystemExit('UI_PORT must be between 1024 and 65535')
    try:
        with single_instance():
            uvicorn.run(create_app(), host='127.0.0.1', port=port, log_level='info')
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
