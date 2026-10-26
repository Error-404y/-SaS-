from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

from database import Database
from delivery import EditDeferred, EditPacer
from test_workflow import BotFixture


class PacerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'verification.json'
        self.db = Database(self.path)
        self.now = 1000.0
        self.pacer = EditPacer(self.db, lambda: self.now)

    def test_reservation_is_durable_before_request_and_survives_restart(self):
        self.pacer.reserve(10)
        restored = EditPacer(Database(self.path), lambda: self.now)
        with self.assertRaises(EditDeferred):
            restored.reserve(10)
        restored.reserve(20)
        self.now += 6
        restored.reserve(10)

    def test_response_reset_headers_delay_next_request_even_without_429(self):
        self.pacer.observe(10, 200, CIMultiDict({'x-ratelimit-remaining': '0', 'x-ratelimit-reset-after': '30'}))
        self.now += 30
        self.assertFalse(self.pacer.ready(10))
        self.now += 0.5
        self.assertTrue(self.pacer.ready(10))

    def test_retry_after_expands_spacing_and_is_not_capped(self):
        self.pacer.observe(10, 429, {'Retry-After': '180'}, {'retry_after': 200})
        self.assertEqual(self.pacer.state(10)['spacing'], 12)
        self.assertEqual(self.pacer.state(10)['next_at'], 1200.5)
        self.pacer.finished(10)
        self.assertEqual(self.pacer.state(10)['next_at'], 1200.5)
        restored = EditPacer(Database(self.path), lambda: self.now)
        self.assertEqual(restored.state(10)['spacing'], 12)

    def test_global_429_blocks_other_channels(self):
        self.pacer.observe(10, 429, {}, {'global': True, 'retry_after': 60})
        with self.assertRaises(EditDeferred):
            self.pacer.reserve(20)

    def test_invalid_numeric_headers_do_not_corrupt_json(self):
        self.pacer.observe(10, 429, {'Retry-After': 'NaN', 'X-RateLimit-Reset-After': 'Infinity'}, {'retry_after': -100})
        self.assertEqual(Database(self.path).edit_limit('10')['next_at'], 1012)

    def test_exhausted_capacity_is_smoothed_not_burst_consumed(self):
        self.pacer.observe(10, 200, {'X-RateLimit-Remaining': '1', 'X-RateLimit-Reset-After': '60'})
        self.assertEqual(self.pacer.state(10)['next_at'], 1030.5)


class DeliveryTests(BotFixture, unittest.IsolatedAsyncioTestCase):
    async def test_deferred_approval_completes_roles_and_dm_without_blocking_staff(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50, submission_dm='done', review_dirty=False)
        self.bot.db.decide(row['id'], 3, 'accepted')
        self.bot.edit_pacer.reserve(self.bot.ids['REVIEW_CHANNEL_ID'])
        with patch('bot.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.bot.process(row['id'])
            sleep.assert_not_awaited()
        current = self.bot.db.get(row['id'])
        self.assertTrue(current['roles_done'])
        self.assertEqual(current['decision_dm'], 'done')
        self.assertTrue(current['review_dirty'])
        self.message.edit.assert_not_awaited()
        self.bot.review_channel.fetch_message.assert_not_awaited()

    async def test_queued_review_uses_latest_state_after_restart(self):
        now = 1000.0
        self.bot.edit_pacer.clock = lambda: now
        self.message.channel.id = self.bot.ids['REVIEW_CHANNEL_ID']
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50, submission_dm='done')
        self.bot.edit_pacer.reserve(self.bot.ids['REVIEW_CHANNEL_ID'])
        await self.bot.sync_review(row['id'])
        self.message.edit.assert_not_awaited()
        self.bot.db.decide(row['id'], 3, 'accepted')
        self.bot.db = Database(self.bot.db.path)
        await self.bot.process(row['id'])
        self.message.edit.assert_not_awaited()
        now += 7
        await self.bot.process(row['id'])
        self.message.edit.assert_awaited_once()
        self.assertIn('Accepted', self.message.edit.call_args.kwargs['embed'].title)
        self.assertIsNone(self.message.edit.call_args.kwargs['view'])
        self.assertFalse(Database(self.bot.db.path).get(row['id'])['review_dirty'])

    async def test_429_trace_updates_shared_budget_without_hiding_response(self):
        params = SimpleNamespace(method='PATCH', url=URL('https://discord.com/api/v10/channels/123/messages/456'),
            response=SimpleNamespace(status=429, headers=CIMultiDict({'Retry-After': '15'}), json=AsyncMock(return_value={'retry_after': 15})))
        await self.bot.observe_edit_response(None, None, params)
        self.assertFalse(self.bot.edit_pacer.ready(123))
        self.assertEqual(self.bot.db.edit_limit('123')['spacing'], 12)
        self.assertFalse(self.bot.edit_pacer.ready(123))

    async def test_non_edit_http_routes_do_not_change_edit_budget(self):
        params = SimpleNamespace(method='GET', url=URL('https://discord.com/api/v10/channels/123/messages/456'))
        await self.bot.observe_edit_response(None, None, params)
        self.assertIsNone(self.bot.db.edit_limit('123'))

    async def test_real_aiohttp_trace_preserves_429_body_for_library_retry(self):
        async def respond(request):
            return web.json_response({'retry_after': 13.5, 'global': False}, status=429,
                                     headers={'Retry-After': '13.5', 'X-RateLimit-Remaining': '1'})
        app = web.Application()
        app.router.add_patch('/api/v10/channels/123/messages/456', respond)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession(trace_configs=[self.bot.http.http_trace]) as session:
                async with session.patch(f'http://127.0.0.1:{port}/api/v10/channels/123/messages/456') as response:
                    self.assertEqual(response.status, 429)
                    self.assertEqual((await response.json())['retry_after'], 13.5)
            self.assertEqual(self.bot.db.edit_limit('123')['spacing'], 12)
            self.assertFalse(self.bot.edit_pacer.ready(123))
        finally:
            await runner.cleanup()

    async def test_deleted_queued_review_is_never_recreated(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50)
        self.bot.edit_pacer.reserve(self.bot.ids['REVIEW_CHANNEL_ID'])
        await self.bot.sync_review(row['id'])
        await self.bot.delete_messages(1, {50})
        self.bot.db = Database(self.bot.db.path)
        await self.bot.process(row['id'])
        self.bot.review_channel.send.assert_not_awaited()
        self.message.edit.assert_not_awaited()
        self.assertTrue(self.bot.db.get(row['id'])['review_deleted'])

    async def test_ui_queue_flush_does_not_repeat_roles_or_dms(self):
        now = 1000.0
        self.bot.edit_pacer.clock = lambda: now
        self.message.channel.id = self.bot.ids['REVIEW_CHANNEL_ID']
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50, submission_dm='done')
        self.bot.edit_pacer.reserve(self.bot.ids['REVIEW_CHANNEL_ID'])
        await self.bot.refresh_messages()
        self.message.edit.assert_not_awaited()
        now += 7
        await self.bot.refresh_messages()
        self.message.edit.assert_awaited_once()
        self.member.add_roles.assert_not_awaited()
        self.user.send.assert_not_awaited()
