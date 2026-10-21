import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from database import Database
from bot import VerificationBot, VerifyView, ReviewView


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/test.sqlite3'
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_duplicate_pending_application(self):
        self.assertIsNotNone(self.db.create(1, 2, 'Player'))
        self.assertIsNone(self.db.create(1, 2, 'PlayerAgain'))
        self.assertIsNotNone(self.db.create(2, 2, 'OtherServer'))

    def test_only_first_reviewer_can_decide(self):
        row = self.db.create(1, 2, 'Player')
        self.assertTrue(self.db.decide(row['id'], 3, 'accepted'))
        self.assertFalse(self.db.decide(row['id'], 4, 'rejected', 'No'))
        self.assertEqual(self.db.get(row['id'])['reviewer_id'], 3)

    def test_rejected_user_can_reapply(self):
        row = self.db.create(1, 2, 'Player')
        self.db.decide(row['id'], 3, 'rejected', 'Wrong name')
        self.assertIsNotNone(self.db.create(1, 2, 'Correct name'))

    def test_restart_preserves_decision_and_work(self):
        row = self.db.create(1, 2, "Player's name")
        self.db.decide(row['id'], 3, 'accepted')
        self.db.close()
        self.db = Database(self.path)
        self.assertEqual(self.db.work()[0]['status'], 'accepted')


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'DATABASE_PATH': self.temp.name + '/test.sqlite3'}):
            self.bot = VerificationBot()
        self.bot.guild = MagicMock(id=1)
        self.member = MagicMock()
        self.member.add_roles = AsyncMock()
        self.member.remove_roles = AsyncMock()
        self.bot.guild.fetch_member = AsyncMock(return_value=self.member)
        self.bot.review_channel = MagicMock()
        self.message = MagicMock(id=50)
        self.message.edit = AsyncMock()
        self.bot.review_channel.send = AsyncMock(return_value=self.message)
        self.bot.review_channel.fetch_message = AsyncMock(return_value=self.message)
        self.user = MagicMock()
        self.user.send = AsyncMock()
        self.bot.fetch_user = AsyncMock(return_value=self.user)

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    async def test_persistent_buttons(self):
        self.assertTrue(VerifyView(self.bot).is_persistent())
        self.assertTrue(ReviewView(self.bot).is_persistent())

    async def test_accept_applies_roles_and_notifies_once(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.decide(row['id'], 3, 'accepted')
        await self.bot.process(row['id'])
        await self.bot.process(row['id'])
        self.member.add_roles.assert_awaited_once()
        self.member.remove_roles.assert_awaited_once()
        self.assertEqual(self.user.send.await_count, 2)
        self.assertEqual(self.bot.db.work(), [])

    async def test_reject_includes_reason_without_changing_roles(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.decide(row['id'], 3, 'rejected', 'Wrong username')
        await self.bot.process(row['id'])
        self.member.add_roles.assert_not_awaited()
        self.assertIn('Wrong username', self.user.send.call_args.kwargs['embed'].description)

    async def test_closed_dms_do_not_block_acceptance(self):
        response = MagicMock(status=403, reason='Forbidden')
        self.user.send.side_effect = discord.Forbidden(response, 'DMs disabled')
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.decide(row['id'], 3, 'accepted')
        await self.bot.process(row['id'])
        result = self.bot.db.get(row['id'])
        self.assertEqual(result['roles_done'], 1)
        self.assertEqual(result['decision_dm'], 'blocked')

    async def test_partial_role_failure_recovers_after_restart(self):
        response = MagicMock(status=403, reason='Forbidden')
        self.member.remove_roles.side_effect = discord.Forbidden(response, 'Missing permissions')
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.decide(row['id'], 3, 'accepted')
        await self.bot.try_process(row['id'])
        self.assertEqual(self.bot.db.get(row['id'])['roles_done'], 0)
        self.member.remove_roles.side_effect = None
        await self.bot.process(row['id'])
        self.assertEqual(self.bot.db.get(row['id'])['roles_done'], 1)

    async def test_staff_role_is_required(self):
        self.bot.configured = True
        self.member.roles = []
        interaction = MagicMock(guild_id=1, channel_id=self.bot.ids['REVIEW_CHANNEL_ID'])
        interaction.user.id = 3
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        self.assertFalse(await self.bot.staff_allowed(interaction))
        interaction.response.send_message.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
