"""Signed MEXC futures API client.

The client deliberately does not retry private requests.  A transport failure
after an order submission is an unknown exchange result and must be reconciled
by ``externalOid`` before any further action.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx


class MexcPrivateError(RuntimeError):
    def __init__(self, message, *, code=None, uncertain=False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


class MexcPrivateClient:
    def __init__(self, http, access_key, secret_key,
                 base_url='https://api.mexc.com', recv_window=10,
                 allow_orders=False, clock=None):
        if not access_key or not secret_key:
            raise ValueError('MEXC API credentials are missing')
        self.http = http
        self.access_key = str(access_key)
        self._secret = str(secret_key).encode('utf-8')
        self.base_url = base_url.rstrip('/')
        self.recv_window = max(1, min(int(recv_window), 30))
        self.allow_orders = bool(allow_orders)
        self.clock = clock or time.time

    @staticmethod
    def _query(params):
        values = [(key, value) for key, value in sorted((params or {}).items())
                  if value is not None]
        return urlencode(values, doseq=True)

    @staticmethod
    def _body(payload):
        return json.dumps(payload or {}, ensure_ascii=False,
                          separators=(',', ':'))

    def _headers(self, timestamp, parameter_string):
        target = f'{self.access_key}{timestamp}{parameter_string}'.encode('utf-8')
        signature = hmac.new(self._secret, target, hashlib.sha256).hexdigest()
        return {
            'ApiKey': self.access_key,
            'Request-Time': str(timestamp),
            'Signature': signature,
            'Recv-Window': str(self.recv_window),
            'Content-Type': 'application/json',
            'Language': 'en-US',
        }

    async def _request(self, method, path, *, params=None, payload=None, trading=False):
        if trading and not self.allow_orders:
            raise MexcPrivateError('Real MEXC order calls are locally disabled')
        method = method.upper()
        timestamp = int(self.clock() * 1000)
        query = self._query(params)
        body = self._body(payload) if method == 'POST' else ''
        parameter_string = body if method == 'POST' else query
        kwargs = {'headers': self._headers(timestamp, parameter_string)}
        if query:
            kwargs['params'] = [(key, value) for key, value in sorted(params.items())
                                if value is not None]
        if method == 'POST':
            kwargs['content'] = body.encode('utf-8')
        try:
            response = await self.http.request(method, self.base_url + path, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise MexcPrivateError(
                'MEXC private request transport failure', uncertain=trading) from error
        if response.status_code != 200:
            raise MexcPrivateError(
                f'MEXC private API HTTP {response.status_code}', uncertain=trading)
        try:
            result = response.json()
        except ValueError as error:
            raise MexcPrivateError('MEXC private API returned invalid JSON',
                                   uncertain=trading) from error
        if not isinstance(result, dict):
            raise MexcPrivateError('MEXC private API returned an invalid envelope',
                                   uncertain=trading)
        code = result.get('code')
        if result.get('success') is not True or code not in (0, '0', None):
            message = str(result.get('message') or result.get('msg') or 'request rejected')
            try:
                uncertain_code = int(code) in (500, 603, 2042)
            except (TypeError, ValueError):
                uncertain_code = False
            raise MexcPrivateError(
                f'MEXC rejected request: {message[:240]}', code=code,
                uncertain=bool(trading and uncertain_code))
        return result.get('data')

    async def assets(self):
        data = await self._request('GET', '/api/v1/private/account/assets')
        if not isinstance(data, list):
            raise MexcPrivateError('MEXC returned invalid account assets')
        return data

    async def open_positions(self, symbol=None):
        data = await self._request(
            'GET', '/api/v1/private/position/open_positions',
            params={'symbol': symbol} if symbol else None)
        if not isinstance(data, list):
            raise MexcPrivateError('MEXC returned invalid open positions')
        return data

    async def open_orders(self, page_num=1, page_size=100):
        data = await self._request(
            'GET', '/api/v1/private/order/list/open_orders',
            params={'page_num': int(page_num), 'page_size': min(int(page_size), 100)})
        if isinstance(data, dict):
            rows = data.get('resultList', data.get('data', []))
        else:
            rows = data
        if not isinstance(rows, list):
            raise MexcPrivateError('MEXC returned invalid open orders')
        return rows

    async def position_mode(self):
        return await self._request('GET', '/api/v1/private/position/position_mode')

    async def order_by_external(self, symbol, external_oid):
        return await self._request(
            'GET', f'/api/v1/private/order/external/{symbol}/{external_oid}')

    async def change_isolated_leverage(self, symbol, position_type, leverage,
                                       position_id=None):
        payload = ({'positionId': str(position_id), 'leverage': int(leverage)}
                   if position_id else {
                       'symbol': symbol, 'positionType': int(position_type),
                       'openType': 1, 'leverage': int(leverage),
                   })
        return await self._request(
            'POST', '/api/v1/private/position/change_leverage', payload=payload,
            trading=True)

    async def create_order(self, payload):
        required = {'symbol', 'price', 'vol', 'side', 'type', 'openType', 'externalOid'}
        if not required.issubset(payload):
            raise ValueError('Incomplete MEXC order payload')
        if int(payload['openType']) != 1:
            raise ValueError('Only isolated margin is allowed')
        return await self._request(
            'POST', '/api/v1/private/order/create', payload=payload, trading=True)

    async def cancel_external(self, symbol, external_oid):
        return await self._request(
            'POST', '/api/v1/private/order/cancel_with_external',
            payload=[{'symbol': symbol, 'externalOid': external_oid}], trading=True)

    async def place_tpsl(self, payload):
        if not payload.get('positionId'):
            raise ValueError('positionId is required for TP/SL')
        return await self._request(
            'POST', '/api/v1/private/stoporder/place', payload=payload, trading=True)
