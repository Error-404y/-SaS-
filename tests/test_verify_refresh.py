import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from database import Database
from test_workflow import BotFixture


class VerificationRefreshStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'verification.json'
        self.db = Database(self.path)

    def test_false_is_noop_and_true_resets_once_with_backup(self):
        row = self.db.create(1, 2, 'Player')
        self.db.decide(row['id'], 3, 'accepted')
        self.db.save_profile(1, 2, {'kind': 'member', 'username': 'Player', 'clan': 'SaS'})
        self.db.queue_join(1, 2)
        self.db.queue_nickname(1, 2)
        self.db.set_pause(True, 'Maintenance', 3)
        original = self.path.read_bytes()
        self.assertFalse(self.db.prepare_verification_refresh(False))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.db.prepare_verification_refresh(True))
        backups = list(self.path.parent.glob('verification.json.before-verify-refresh-*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertFalse(self.db.is_verified(1, 2))
        self.assertEqual(self.db.applications(), [])
        self.assertIsNone(self.db.profile(1, 2))
        self.assertEqual(self.db.joins(), [])
        self.assertEqual(self.db.nickname_jobs(), [])
        self.assertFalse(self.db.paused()['active'])
        self.assertFalse(self.db.claim_refresh_notice())
        reloaded = Database(self.path)
        self.assertTrue(reloaded.prepare_verification_refresh(True))
        reloaded.finish_verification_refresh()
        self.assertFalse(reloaded.prepare_verification_refresh(True))
        self.assertTrue(reloaded.claim_refresh_notice())
        self.assertFalse(reloaded.claim_refresh_notice())
        self.assertEqual(len(list(self.path.parent.glob('verification.json.before-verify-refresh-*.bak'))), 1)
        self.assertFalse(reloaded.prepare_verification_refresh(False))
        self.assertFalse(reloaded.data['verification_refresh']['latched'])
        self.assertTrue(reloaded.prepare_verification_refresh(True))
        self.assertEqual(len(list(self.path.parent.glob('verification.json.before-verify-refresh-*.bak'))), 2)

    def test_failed_backup_does_not_reset_database(self):
        row = self.db.create(1, 2, 'Player')
        before = self.path.read_bytes()
        with patch('database.shutil.copy2', side_effect=OSError('backup failed')):
            with self.assertRaises(OSError):
                self.db.prepare_verification_refresh(True)
        self.assertEqual(before, self.path.read_bytes())
        self.assertIsNotNone(self.db.get(row['id']))


class VerificationRefreshBotTests(BotFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.verify_refresh_enabled = True
        ids = self.bot.ids
        roles = {role_id: MagicMock(id=role_id)
                 for role_id in (ids['MEMBER_ROLE_ID'], ids['ALLIES_ROLE_ID'], ids['PROGRESS_ROLE_ID'])}
        self.bot.guild.get_role.side_effect = lambda role_id: roles[role_id]
        self.member.roles = [roles[ids['MEMBER_ROLE_ID']], roles[ids['ALLIES_ROLE_ID']], MagicMock(id=888)]
        async def remove_roles(*role_values, **kwargs):
            removed = {role.id for role in role_values}
            self.member.roles = [role for role in self.member.roles if role.id not in removed]
        self.refresh_remove_roles = remove_roles
        self.member.remove_roles.side_effect = remove_roles
        async def all_members(limit=None):
            yield self.member
        self.bot.guild.fetch_members = all_members
        self.bot.verify_channel = MagicMock()
        self.bot.verify_channel.permissions_for.return_value.mention_everyone = True
        self.bot.verify_channel.send = AsyncMock()

    async def test_removes_both_verification_roles_but_preserves_staff_and_pings_once(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.decide(row['id'], 3, 'accepted')
        self.assertTrue(self.bot.db.prepare_verification_refresh(True))
        await self.bot.refresh_verification_roles()
        self.assertEqual({r.id for r in self.member.roles},
                         {888, self.bot.ids['PROGRESS_ROLE_ID']})
        self.assertFalse(self.bot.db.is_verified(1, 2))
        self.assertEqual(self.member.remove_roles.await_count, 1)
        self.assertEqual(len(self.member.remove_roles.call_args.args), 2)
        await self.bot.notify_verification_refresh()
        await self.bot.notify_verification_refresh()
        self.bot.verify_channel.send.assert_awaited_once()
        args = self.bot.verify_channel.send.call_args
        self.assertIn('@everyone', args.args[0])
        self.assertIn('verify again', args.args[0])
        self.assertTrue(args.kwargs['allowed_mentions'].everyone)
        self.assertFalse(args.kwargs['allowed_mentions'].roles)
        self.assertFalse(self.bot.db.prepare_verification_refresh(True))
        self.assertEqual(len(self.bot.db.applications()), 0)

    async def test_failed_role_removal_prevents_notice_until_retry(self):
        self.assertTrue(self.bot.db.prepare_verification_refresh(True))
        self.member.remove_roles.side_effect = discord.Forbidden(
            MagicMock(status=403, reason='Forbidden'), 'Insufficient permissions')
        with self.assertRaises(RuntimeError):
            await self.bot.refresh_verification_roles()
        self.assertTrue(self.bot.db.data['verification_refresh']['pending'])
        self.assertFalse(self.bot.db.claim_refresh_notice())
        self.member.remove_roles.side_effect = self.refresh_remove_roles
        await self.bot.refresh_verification_roles()
        await self.bot.notify_verification_refresh()
        self.bot.verify_channel.send.assert_awaited_once()

    async def test_disabled_flag_never_notifies(self):
        self.bot.verify_refresh_enabled = False
        self.assertFalse(self.bot.db.prepare_verification_refresh(False))
        await self.bot.notify_verification_refresh()
        self.bot.verify_channel.send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
