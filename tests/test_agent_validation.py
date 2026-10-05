import json
import tempfile
import unittest
from pathlib import Path
import httpx

from bootstrap import SetupError
from trader.agent import Agent
from trader.store import Store
from tests.test_recognition import signal


class AgentValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db.sqlite3')
        self.payload = {'messages': [{'id': 2, 'text': 'Пробуем', 'media': []}], 'parents': []}
        self.cfg = {'OPENAI_MODEL': 'gpt-5.6-luna', 'OPENAI_API_KEY': 'test-only', 'LLM_MONTHLY_BUDGET_USD': '20'}

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def response(self, **changes):
        s = signal().model_dump()
        s.update(changes)
        return httpx.Response(200, json={'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 50},
            'output': [{'type': 'function_call', 'name': 'submit_analysis', 'call_id': 'call-test',
                        'arguments': json.dumps({'summary': 'test', 'signals': [s]})}]})

    async def test_one_repair_fixes_unit_suffix_and_records_error(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return self.response(leverage_min='20x', leverage_max='20x') if len(requests) == 1 else self.response()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result, trace = await Agent(self.cfg, client, self.store).analyze(self.payload, 1, 2)
        self.assertEqual(result.signals[0].proposed_leverage(), 20)
        self.assertEqual(len(requests), 2)
        self.assertEqual(trace, ['submit_analysis', 'submit_analysis'])
        feedback = requests[1]['input'][-1]
        self.assertEqual(feedback['type'], 'function_call_output')
        self.assertIn('leverage_min', feedback['output'])
        self.assertNotIn('test-only', feedback['output'])
        attempt = self.store.db.execute('SELECT validation_errors FROM agent_attempts ORDER BY id LIMIT 1').fetchone()
        self.assertIn('leverage_min', attempt[0])

    async def test_repeated_invalid_response_stops_after_two_requests(self):
        calls = []
        def handler(request):
            calls.append(1)
            return self.response(leverage_min='20x')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(SetupError):
                await Agent(self.cfg, client, self.store).analyze(self.payload, 1, 2)
        self.assertEqual(len(calls), 2)

    async def test_self_link_is_corrected(self):
        calls = []
        def handler(request):
            calls.append(1)
            return self.response(related_message_id=2 if len(calls) == 1 else None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result, _ = await Agent(self.cfg, client, self.store).analyze(self.payload, 1, 2)
        self.assertIsNone(result.signals[0].related_message_id)
        self.assertEqual(len(calls), 2)
