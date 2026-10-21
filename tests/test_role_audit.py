import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

import test_bot
from time_utils import zurich_time
from views import ApplicationModal, ReviewView


class RoleAuditTests(test_bot.BotFixture, unittest.IsolatedAsyncioTestCase):
    def state(self, verified=False):
        self.bot.db.track_member(1, 2, verified)
        return self.bot.db.member_state(1, 2)

    def role_events(self):
        return [e for e in self.bot.db.events() if e['action'] == 'roles']

    async def test_restart_with_correct_roles_is_silent(self):
        await self.bot.sync_roles(self.state())
        await self.bot.sync_roles(self.state())
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.assertEqual(self.role_events(), [])
        self.assertFalse(self.bot.db.member_state(1, 2)['dirty'])

    async def test_confirmed_change_is_logged_once(self):
        await self.bot.sync_roles(self.state(True))
        await self.bot.sync_roles(self.state(True))
        self.assertEqual(len(self.role_events()), 1)
        event = self.role_events()[0]
        self.assertEqual(event['added'], [self.bot.ids['MEMBER_ROLE_ID']])
        self.assertEqual(event['removed'], [self.bot.ids['PROGRESS_ROLE_ID']])
        self.assertEqual(event['subject_user_id'], 2)
        self.assertTrue(event['confirmed'])
        self.member.add_roles.assert_awaited_once()
        self.member.remove_roles.assert_awaited_once()

    async def test_http_success_without_actual_roles_is_not_logged(self):
        self.member.add_roles.side_effect = None
        self.member.remove_roles.side_effect = None
        await self.bot.sync_roles(self.state(True))
        self.assertEqual(self.role_events(), [])
        self.assertTrue(self.bot.db.member_state(1, 2)['dirty'])

    async def test_failed_update_with_no_change_is_not_logged(self):
        self.member.add_roles.side_effect = discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'Denied')
        with self.assertRaises(discord.Forbidden):
            await self.bot.sync_roles(self.state(True))
        self.assertEqual(self.role_events(), [])
        self.assertTrue(self.bot.db.member_state(1, 2)['dirty'])

    async def test_partial_update_logs_only_confirmed_part_then_remaining_part(self):
        state = self.state(True)
        self.member.remove_roles.side_effect = discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'Denied')
        with self.assertRaises(discord.Forbidden):
            await self.bot.sync_roles(state)
        self.assertEqual(self.role_events()[0]['removed'], [])
        self.assertEqual(self.role_events()[0]['added'], [self.bot.ids['MEMBER_ROLE_ID']])
        self.member.remove_roles.side_effect = self.remove_role
        await self.bot.sync_roles(self.bot.db.member_state(1, 2))
        self.assertEqual(len(self.role_events()), 2)
        self.assertEqual(self.role_events()[1]['added'], [])
        self.assertEqual(self.role_events()[1]['removed'], [self.bot.ids['PROGRESS_ROLE_ID']])
        self.member.add_roles.assert_awaited_once()

    async def test_no_confirmation_fetch_means_no_success_claim(self):
        self.bot.guild.fetch_member.side_effect = [self.member, discord.HTTPException(MagicMock(status=503, reason='Unavailable'), 'Retry')]
        with self.assertRaises(discord.HTTPException):
            await self.bot.sync_roles(self.state(True))
        self.assertEqual(self.role_events(), [])
        self.assertTrue(self.bot.db.member_state(1, 2)['dirty'])

    async def test_admin_can_accept_own_request_without_high_rank(self):
        self.bot.configured = True
        row = self.bot.db.create(1, 2, 'Admin')
        self.bot.db.update(row['id'], message_id=50)
        self.member.id = 2
        self.member.guild_permissions.administrator = True
        interaction = MagicMock(guild_id=1, channel_id=self.bot.ids['REVIEW_CHANNEL_ID'])
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = 2
        interaction.user.roles = []
        interaction.user.guild_permissions.administrator = True
        interaction.message.id = 50
        interaction.response.defer = AsyncMock()
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()
        view = ReviewView(self.bot)
        await view.accept.callback(interaction)
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'accepted')
        self.assertEqual(self.bot.db.get(row['id'])['roles_done'], 1)

    async def test_unprivileged_applicant_cannot_accept_own_request(self):
        self.bot.configured = True
        row = self.bot.db.create(1, 2, 'Player')
        interaction = MagicMock(guild_id=1, channel_id=self.bot.ids['REVIEW_CHANNEL_ID'])
        interaction.user.id = 2
        interaction.response.defer = AsyncMock()
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()
        await self.bot.apply_action(interaction, row, 'accepted')
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'pending')

    async def test_default_form_only_asks_username(self):
        self.bot.extra_questions = False
        self.assertEqual(len(ApplicationModal(self.bot, 'en', 'Rules').children), 1)

    async def test_review_only_enables_valid_actions(self):
        view = ReviewView(self.bot, 'accepted')
        self.assertTrue(view.accept.disabled)
        self.assertTrue(view.claim.disabled)
        self.assertFalse(view.undo.disabled)
        self.assertFalse(view.revoke.disabled)

    async def test_audit_has_no_placeholder_fields(self):
        await self.bot.sync_roles(self.state(True))
        result = self.bot.audit_embed(self.role_events()[0], None)
        fields = {f.name: f.value for f in result.fields}
        self.assertEqual(fields['Member'], '<@2>')
        self.assertNotIn('Application', fields)
        self.assertIn('Time (Zurich)', fields)
        self.assertNotIn('None', str(result.to_dict()))


class ZurichTests(unittest.TestCase):
    def test_summer_and_winter_and_legacy_utc(self):
        self.assertEqual(zurich_time('2026-07-01 12:00:00'), '01.07.2026 14:00:00 CEST')
        self.assertEqual(zurich_time('2026-01-01T12:00:00+00:00'), '01.01.2026 13:00:00 CET')
        self.assertEqual(zurich_time('2026-10-21 19:36:32'), '21.10.2026 21:36:32 CEST')
