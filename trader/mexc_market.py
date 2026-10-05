"""Read-only MEXC futures market checks.

This module deliberately contains no authentication and cannot place orders.  It
normalizes an internal ticker, validates the public contract specification, and
estimates the order volume that a later executor would have to submit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import re
import time

import httpx


BASE_URL = 'https://api.mexc.com'
CHECKABLE_ACTIONS = ('open', 'add')
APPROVABLE_STATUSES = ('valid', 'specs_only', 'stale_review')


class MarketDataError(RuntimeError):
    """A public MEXC response could not be used safely."""


class MarketValidationError(ValueError):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _decimal(value, field, *, allow_zero=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise MarketDataError(f'MEXC вернул некорректное поле {field}') from None
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise MarketDataError(f'MEXC вернул некорректное поле {field}')
    return number


def _signed_decimal(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise MarketDataError(f'MEXC вернул некорректное поле {field}') from None
    if not number.is_finite():
        raise MarketDataError(f'MEXC вернул некорректное поле {field}')
    return number


def _text(number):
    if number is None:
        return None
    value = format(Decimal(number).normalize(), 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def normalize_symbol(symbol: str) -> str:
    raw = str(symbol or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9]{2,35}', raw) or not raw.endswith('USDT'):
        raise MarketValidationError('contract_not_found', 'Поддерживается только тикер вида BASEUSDT')
    base = raw[:-4]
    if not base:
        raise MarketValidationError('contract_not_found', 'Базовый актив не определён')
    return f'{base}_USDT'


def _contract_snapshot(contract):
    fields = (
        'symbol', 'displayName', 'displayNameEn', 'positionOpenType', 'futureType',
        'contractSize', 'minLeverage', 'maxLeverage', 'countryConfigContractMaxLeverage',
        'priceUnit', 'volUnit', 'minVol', 'maxVol', 'state', 'apiAllowed',
        'settleCoin', 'quoteCoin', 'baseCoin', 'riskLimitMode', 'riskLimitCustom',
    )
    return {key: contract.get(key) for key in fields if key in contract}


def _ticker_snapshot(ticker):
    fields = ('symbol', 'lastPrice', 'bid1', 'ask1', 'indexPrice', 'fairPrice', 'timestamp')
    return {key: ticker.get(key) for key in fields if key in ticker}


def _effective_max_leverage(contract):
    values = [_decimal(contract.get('maxLeverage'), 'maxLeverage')]
    country = contract.get('countryConfigContractMaxLeverage')
    if country not in (None, '', 0, '0'):
        values.append(_decimal(country, 'countryConfigContractMaxLeverage'))
    return min(values)


def _multiple(value, unit):
    if unit == 0:
        return True
    return value % unit == 0


def validate_contract(contract):
    try:
        state = int(contract.get('state', -1))
        future_type = int(contract.get('futureType', -1))
        position_type = int(contract.get('positionOpenType', 0))
    except (TypeError, ValueError):
        raise MarketDataError('MEXC вернул неполную спецификацию контракта') from None
    if state != 0:
        raise MarketValidationError('inactive', 'Фьючерсный контракт MEXC сейчас неактивен')
    if contract.get('apiAllowed') is not True:
        raise MarketValidationError('api_disabled', 'MEXC не разрешает API-торговлю этим контрактом')
    if future_type != 1:
        raise MarketValidationError('unsupported_contract', 'Контракт не является бессрочным фьючерсом')
    if str(contract.get('settleCoin') or 'USDT').upper() != 'USDT':
        raise MarketValidationError('unsupported_contract', 'Контракт рассчитывается не в USDT')
    if position_type not in (1, 3):
        raise MarketValidationError('isolated_unsupported', 'Контракт не поддерживает изолированную маржу')


def validate_order_parameters(contract, ticker, values):
    """Validate plan values against cached public data and return an estimate."""
    validate_contract(contract)
    action = values.get('action')
    leverage = values.get('effective_leverage')
    margin = values.get('margin_usdt')

    min_leverage = _decimal(contract.get('minLeverage'), 'minLeverage')
    max_leverage = _effective_max_leverage(contract)
    if leverage in (None, '') or margin in (None, ''):
        return {
            'min_leverage': _text(min_leverage), 'max_leverage': _text(max_leverage),
            'estimated_contracts': None, 'estimated_base_quantity': None,
            'estimated_notional_usdt': None,
        }
    chosen_leverage = _decimal(leverage, 'leverage')
    if chosen_leverage != chosen_leverage.to_integral_value():
        raise MarketValidationError('leverage_unsupported', 'Плечо должно быть целым числом')
    if chosen_leverage < min_leverage or chosen_leverage > max_leverage:
        raise MarketValidationError(
            'leverage_unsupported',
            f'MEXC допускает плечо от {_text(min_leverage)}× до {_text(max_leverage)}×')

    price_unit = _decimal(contract.get('priceUnit'), 'priceUnit')
    if values.get('order_kind') == 'trigger_limit':
        for field, label in (('trigger_price', 'Trigger'), ('limit_price', 'Limit')):
            price = _decimal(values.get(field), field)
            if not _multiple(price, price_unit):
                raise MarketValidationError(
                    'price_step_mismatch', f'{label}-цена не кратна шагу MEXC {_text(price_unit)}')

    if action not in CHECKABLE_ACTIONS:
        return {
            'min_leverage': _text(min_leverage), 'max_leverage': _text(max_leverage),
            'estimated_contracts': None, 'estimated_base_quantity': None,
            'estimated_notional_usdt': None,
        }
    chosen_margin = _decimal(margin, 'margin_usdt')
    side = values.get('side')
    preferred = ticker.get('ask1') if side == 'long' else ticker.get('bid1')
    market_price = _decimal(preferred or ticker.get('lastPrice'), 'market price')
    contract_size = _decimal(contract.get('contractSize'), 'contractSize')
    volume_unit = _decimal(contract.get('volUnit'), 'volUnit')
    minimum_volume = _decimal(contract.get('minVol'), 'minVol')
    maximum_volume = _decimal(contract.get('maxVol'), 'maxVol')
    raw_contracts = chosen_margin * chosen_leverage / market_price / contract_size
    contracts = (raw_contracts / volume_unit).to_integral_value(rounding=ROUND_FLOOR) * volume_unit
    if contracts < minimum_volume:
        minimum_margin = minimum_volume * contract_size * market_price / chosen_leverage
        raise MarketValidationError(
            'below_min_volume',
            f'Маржи недостаточно: минимум около {_text(minimum_margin)} USDT при {int(chosen_leverage)}×')
    if contracts > maximum_volume:
        raise MarketValidationError('above_max_volume', 'Расчётный объём превышает максимум MEXC')
    if contract.get('riskLimitMode') == 'CUSTOM' and isinstance(
            contract.get('riskLimitCustom'), list):
        tiers = sorted(
            (tier for tier in contract['riskLimitCustom'] if isinstance(tier, dict)),
            key=lambda tier: _decimal(tier.get('maxVol'), 'riskLimitCustom.maxVol'))
        tier = next((item for item in tiers
                     if contracts <= _decimal(item.get('maxVol'), 'riskLimitCustom.maxVol')), None)
        if tier is None:
            raise MarketValidationError('above_max_volume', 'Объём не помещается в риск-лимиты MEXC')
        tier_leverage = _decimal(tier.get('maxLeverage'), 'riskLimitCustom.maxLeverage')
        if chosen_leverage > tier_leverage:
            raise MarketValidationError(
                'leverage_unsupported',
                f'Для расчётного объёма MEXC допускает не более {_text(tier_leverage)}×')
    base_quantity = contracts * contract_size
    return {
        'min_leverage': _text(min_leverage), 'max_leverage': _text(max_leverage),
        'estimated_contracts': _text(contracts),
        'estimated_base_quantity': _text(base_quantity),
        'estimated_notional_usdt': _text(base_quantity * market_price),
    }


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _kline_interval(age_seconds):
    if age_seconds <= 2000 * 60:
        return 'Min1'
    if age_seconds <= 2000 * 5 * 60:
        return 'Min5'
    if age_seconds <= 2000 * 15 * 60:
        return 'Min15'
    if age_seconds <= 2000 * 60 * 60:
        return 'Min60'
    return 'Day1'


def _levels(values, key):
    raw = values.get(key)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    return [_decimal(value, key) for value in (raw or [])]


class MexcMarketClient:
    def __init__(self, http: httpx.AsyncClient, base_url=BASE_URL):
        self.http = http
        self.base_url = base_url.rstrip('/')
        self._contracts = {}

    async def _get(self, path, params=None):
        try:
            response = await self.http.get(self.base_url + path, params=params, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise MarketDataError(f'Публичный API MEXC недоступен: {type(error).__name__}') from None
        if not isinstance(payload, dict) or payload.get('success') is not True:
            message = str(payload.get('message') or payload.get('code') or 'invalid response')[:160]
            raise MarketDataError(f'MEXC отклонил запрос: {message}')
        return payload.get('data')

    async def contract(self, symbol):
        cached = self._contracts.get(symbol)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        try:
            data = await self._get('/api/v1/contract/detail/country', {'symbol': symbol})
        except MarketDataError as error:
            if 'Contract not exists' in str(error) or 'not exist' in str(error).lower():
                return None
            raise
        if isinstance(data, list):
            contract = next((item for item in data if item.get('symbol') == symbol), None)
        elif isinstance(data, dict):
            contract = data if data.get('symbol') == symbol else None
        else:
            raise MarketDataError('MEXC вернул контракт в неизвестном формате')
        if contract:
            self._contracts[symbol] = (time.monotonic(), contract)
        return contract

    async def ticker(self, symbol):
        data = await self._get('/api/v1/contract/ticker', {'symbol': symbol})
        if not isinstance(data, dict) or data.get('symbol') != symbol:
            raise MarketDataError('MEXC не вернул котировку нужного контракта')
        return data

    async def kline(self, symbol, start, end, interval):
        data = await self._get(f'/api/v1/contract/kline/{symbol}', {
            'interval': interval, 'start': int(start), 'end': int(end)})
        if not isinstance(data, dict):
            raise MarketDataError('MEXC вернул свечи в неизвестном формате')
        highs, lows = data.get('high'), data.get('low')
        if not isinstance(highs, list) or not isinstance(lows, list) or not highs or len(highs) != len(lows):
            raise MarketDataError('MEXC не вернул свечи за время сигнала')
        return data

    async def candle_rows(self, symbol, start, end, interval='Min1'):
        """Return validated public candles in chronological order."""
        data = await self.kline(symbol, start, end, interval)
        fields = ('time', 'open', 'high', 'low', 'close')
        arrays = {field: data.get(field) for field in fields}
        if any(not isinstance(values, list) for values in arrays.values()):
            raise MarketDataError('MEXC вернул неполные свечи для восстановления')
        lengths = {len(values) for values in arrays.values()}
        if len(lengths) != 1 or not lengths or not next(iter(lengths)):
            raise MarketDataError('MEXC вернул несогласованные свечи для восстановления')
        rows = []
        for index in range(next(iter(lengths))):
            try:
                timestamp = int(arrays['time'][index])
            except (TypeError, ValueError):
                raise MarketDataError('MEXC вернул некорректное время свечи') from None
            rows.append({
                'time': timestamp,
                'open': _text(_decimal(arrays['open'][index], 'candle.open')),
                'high': _text(_decimal(arrays['high'][index], 'candle.high')),
                'low': _text(_decimal(arrays['low'][index], 'candle.low')),
                'close': _text(_decimal(arrays['close'][index], 'candle.close')),
            })
        return sorted(rows, key=lambda item: item['time'])

    async def funding_history(self, symbol, page_size=100):
        """Return validated public funding settlements, oldest first."""
        data = await self._get('/api/v1/contract/funding_rate/history', {
            'symbol': symbol, 'page_num': 1, 'page_size': max(1, min(int(page_size), 1000)),
        })
        if not isinstance(data, dict) or not isinstance(data.get('resultList'), list):
            raise MarketDataError('MEXC вернул историю funding в неизвестном формате')
        result = []
        for item in data['resultList']:
            if not isinstance(item, dict) or item.get('symbol') != symbol:
                raise MarketDataError('MEXC вернул funding другого контракта')
            rate = _signed_decimal(item.get('fundingRate'), 'fundingRate')
            try:
                settle_ms = int(item.get('settleTime'))
                settled = datetime.fromtimestamp(settle_ms / 1000, tz=timezone.utc)
            except (OSError, OverflowError, TypeError, ValueError):
                raise MarketDataError('MEXC вернул некорректное поле settleTime') from None
            result.append({
                'symbol': symbol, 'rate': _text(rate),
                'settle_time': settled.isoformat(),
            })
        return sorted(result, key=lambda item: item['settle_time'])

    async def check_plan(self, plan, event_time=None, now=None):
        checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        result = {
            'status': 'error', 'reason': 'Проверка MEXC не завершена',
            'mexc_symbol': None, 'contract': {}, 'ticker': {}, 'candles': {},
            'current_price': None, 'bid_price': None, 'ask_price': None,
            'estimated_contracts': None, 'estimated_base_quantity': None,
            'estimated_notional_usdt': None, 'observed_high': None, 'observed_low': None,
            'signal_age_seconds': None, 'checked_at': checked_at.isoformat(),
        }
        try:
            symbol = normalize_symbol(plan.get('symbol'))
            result['mexc_symbol'] = symbol
            contract = await self.contract(symbol)
            if not contract:
                raise MarketValidationError(
                    'contract_not_found', f'Контракт {symbol} не найден на MEXC')
            result['contract'] = _contract_snapshot(contract)
            validate_contract(contract)
            ticker = await self.ticker(symbol)
            result['ticker'] = _ticker_snapshot(ticker)
            result['current_price'] = _text(_decimal(ticker.get('lastPrice'), 'lastPrice'))
            if ticker.get('bid1') not in (None, ''):
                result['bid_price'] = _text(_decimal(ticker['bid1'], 'bid1'))
            if ticker.get('ask1') not in (None, ''):
                result['ask_price'] = _text(_decimal(ticker['ask1'], 'ask1'))
            estimate = validate_order_parameters(contract, ticker, plan)
            result.update(estimate)

            signal_time = _parse_time(event_time)
            age = max(0, int((checked_at - signal_time).total_seconds())) if signal_time else 0
            result['signal_age_seconds'] = age
            missing = plan.get('effective_leverage') in (None, '') or plan.get('margin_usdt') in (None, '')
            result.update(status='specs_only' if missing else 'valid', reason=(
                'Контракт доступен; укажите плечо и маржу для расчёта объёма'
                if missing else 'Контракт, плечо, шаги цены и расчётный объём прошли проверку MEXC'))

            if age > 600:
                interval = _kline_interval(age)
                candles = await self.kline(
                    symbol, signal_time.timestamp(), checked_at.timestamp(), interval)
                highs = [_decimal(value, 'kline.high') for value in candles['high']]
                lows = [_decimal(value, 'kline.low') for value in candles['low']]
                observed_high, observed_low = max(highs), min(lows)
                result['observed_high'], result['observed_low'] = _text(observed_high), _text(observed_low)
                times = candles.get('time') or []
                result['candles'] = {
                    'interval': interval, 'points': len(highs),
                    'first_time': times[0] if times else None,
                    'last_time': times[-1] if times else None,
                }
                stop = (_decimal(plan.get('stop_price'), 'stop_price')
                        if plan.get('stop_price') not in (None, '') else None)
                take_profits = _levels(plan, 'take_profits_json')
                side = plan.get('side')
                stop_hit = bool(stop and ((side == 'long' and observed_low <= stop)
                                          or (side == 'short' and observed_high >= stop)))
                target_hit = any((side == 'long' and observed_high >= target)
                                 or (side == 'short' and observed_low <= target)
                                 for target in take_profits)
                if stop_hit or target_hit:
                    what = 'стоп' if stop_hit else 'тейк-профит'
                    result.update(
                        status='scenario_finished',
                        reason=f'После сигнала цена уже пересекла авторский {what}; вход заблокирован')
                else:
                    result.update(
                        status='stale_review',
                        reason='Сигнал старше 10 минут: уровни не пересечены, но актуальность должен подтвердить оператор')
        except MarketValidationError as error:
            result.update(status=error.status, reason=error.reason)
        except MarketDataError as error:
            result.update(status='error', reason=str(error)[:500])
        return result
