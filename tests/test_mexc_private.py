import hashlib
import hmac
import json
import unittest

import httpx

from trader.mexc_private import MexcPrivateClient, MexcPrivateError


class MexcPrivateClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_signature_uses_sorted_query_and_required_headers(self):
        seen = {}

        def handler(request):
            seen['request'] = request
            return httpx.Response(200, json={'success': True, 'code': 0, 'data': []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = MexcPrivateClient(
                http, 'access', 'secret', 'https://test.mexc', clock=lambda: 1.234)
            await client._request('GET', '/private', params={'z': 2, 'a': 1})
        request = seen['request']
        self.assertEqual(request.url.query.decode(), 'a=1&z=2')
        expected = hmac.new(
            b'secret', b'access1234a=1&z=2', hashlib.sha256).hexdigest()
        self.assertEqual(request.headers['Signature'], expected)
        self.assertEqual(request.headers['Request-Time'], '1234')
        self.assertNotIn('secret', str(request.headers).lower())

    async def test_post_signature_matches_exact_compact_body(self):
        seen = {}

        def handler(request):
            seen['request'] = request
            return httpx.Response(200, json={
                'success': True, 'code': 0, 'data': {'orderId': '42'}})

        payload = {'symbol': 'AVA_USDT', 'price': '0', 'vol': '1',
                   'side': 3, 'type': 5, 'openType': 1, 'externalOid': 'tgt-1'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = MexcPrivateClient(
                http, 'access', 'secret', 'https://test.mexc',
                allow_orders=True, clock=lambda: 2)
            result = await client.create_order(payload)
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        request = seen['request']
        self.assertEqual(request.content, body)
        expected = hmac.new(b'secret', b'access2000' + body, hashlib.sha256).hexdigest()
        self.assertEqual(request.headers['Signature'], expected)
        self.assertEqual(result['orderId'], '42')

    async def test_writes_are_guarded_and_transport_failure_is_uncertain(self):
        payload = {'symbol': 'AVA_USDT', 'price': '0', 'vol': '1',
                   'side': 3, 'type': 5, 'openType': 1, 'externalOid': 'tgt-1'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200))) as http:
            client = MexcPrivateClient(http, 'a', 'b', allow_orders=False)
            with self.assertRaisesRegex(MexcPrivateError, 'disabled'):
                await client.create_order(payload)

        calls = 0

        def broken(request):
            nonlocal calls
            calls += 1
            raise httpx.ConnectError('offline', request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http:
            client = MexcPrivateClient(http, 'a', 'b', allow_orders=True)
            with self.assertRaises(MexcPrivateError) as caught:
                await client.create_order(payload)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(calls, 1)

    async def test_cross_margin_payload_is_rejected_locally(self):
        async with httpx.AsyncClient() as http:
            client = MexcPrivateClient(http, 'a', 'b', allow_orders=True)
            with self.assertRaisesRegex(ValueError, 'isolated'):
                await client.create_order({
                    'symbol': 'AVA_USDT', 'price': '0', 'vol': '1', 'side': 3,
                    'type': 5, 'openType': 2, 'externalOid': 'x'})

    async def test_exchange_500_on_write_is_an_unknown_result(self):
        def handler(request):
            return httpx.Response(200, json={
                'success': False, 'code': 500, 'message': 'system error'})

        payload = {'symbol': 'AVA_USDT', 'price': '0', 'vol': '1',
                   'side': 3, 'type': 5, 'openType': 1, 'externalOid': 'tgt-1'}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = MexcPrivateClient(http, 'a', 'b', allow_orders=True)
            with self.assertRaises(MexcPrivateError) as caught:
                await client.create_order(payload)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.code, 500)


if __name__ == '__main__':
    unittest.main()
