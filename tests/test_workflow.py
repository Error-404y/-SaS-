import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot import VerificationBot
from database import Database
from views import ReviewView, VerifyView, ApplicationModal


class BotFixture:
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'DATABASE_PATH': self.temp.name + '/verification.json'}):
            self.bot = VerificationBot()
        self.bot.guild = MagicMock(id=1)
        self.bot.guild.get_role.side_effect = lambda role_id: role_id
        self.bot.configured = True
        self.member = MagicMock(id=2)
        self.member.roles = [MagicMock(id=self.bot.ids['PROGRESS_ROLE_ID'])]
        self.member.guild_permissions.administrator = False
        self.member.nick = None
        async def edit_member(**kwargs):
            self.member.nick = kwargs['nick']
            return self.member
        self.member.edit = AsyncMock(side_effect=edit_member)
        self.member.bot = False
        self.member.guild.id = 1
        self.role_ids = lambda: {r.id for r in self.member.roles}
        async def add_role(role_id, **kwargs):
            if role_id not in self.role_ids():
                self.member.roles.append(MagicMock(id=role_id))
        async def remove_role(role_id, **kwargs):
            self.member.roles = [r for r in self.member.roles if r.id != role_id]
        self.add_role, self.remove_role = add_role, remove_role
        self.member.add_roles = AsyncMock(side_effect=add_role)
        self.member.remove_roles = AsyncMock(side_effect=remove_role)
        self.bot.guild.fetch_member = AsyncMock(return_value=self.member)
        self.message = MagicMock(id=50)
        self.message.edit = AsyncMock()
        self.bot.review_channel = MagicMock()
        self.bot.review_channel.send = AsyncMock(return_value=self.message)
        self.bot.review_channel.fetch_message = AsyncMock(return_value=self.message)
        self.bot.log_channel = MagicMock()
        self.log_message = MagicMock(id=60)
        self.log_message.edit = AsyncMock()
        self.bot.log_channel.send = AsyncMock(return_value=self.log_message)
        self.bot.log_channel.fetch_message = AsyncMock(return_value=self.log_message)
        async def empty_history(**kwargs):
            if False:
                yield None
        self.bot.review_channel.history = empty_history
        self.bot.log_channel.history = empty_history
        self.user = MagicMock()
        self.user.send = AsyncMock()
        self.bot.fetch_user = AsyncMock(return_value=self.user)

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    def interaction(self, user_id=2, public=False):
        interaction = MagicMock(guild_id=1, channel_id=self.bot.ids['VERIFY_CHANNEL_ID' if public else 'REVIEW_CHANNEL_ID'])
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = user_id
        interaction.user.roles = []
        interaction.user.guild_permissions.administrator = False
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.send_modal = AsyncMock()
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()
        interaction.message.id = 50
        return interaction

    def accepted(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.decide(row['id'], 3, 'accepted')
        return row['id']

    def denied(self):
        return discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'Denied')

    def missing(self):
        return discord.NotFound(MagicMock(status=404, reason='Missing'), 'Deleted')


class WorkflowTests(BotFixture, unittest.IsolatedAsyncioTestCase):
    async def test_only_verify_and_accept_reject_controls_exist(self):
        self.assertEqual([c.label for c in VerifyView(self.bot).children], ['Verify - Member', 'Verify - Allies'])
        self.assertEqual([c.label for c in ReviewView(self.bot).children], ['Accept', 'Reject'])
        self.assertEqual(len(ApplicationModal(self.bot).children), 1)
        self.assertTrue(ReviewView(self.bot).is_persistent())
        self.assertTrue(VerifyView(self.bot).is_persistent())

    async def test_acceptance_logs_only_complete_transfer_once(self):
        application_id = self.accepted()
        await self.bot.process(application_id)
        await self.bot.process(application_id)
        await self.bot.recover()
        await self.bot.recover()
        self.assertIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['PROGRESS_ROLE_ID'], self.role_ids())
        self.member.add_roles.assert_awaited_once()
        self.member.remove_roles.assert_awaited_once()
        self.bot.log_channel.send.assert_awaited_once()
        self.log_message.edit.assert_not_awaited()
        self.assertEqual(self.user.send.await_count, 2)
        self.assertEqual(len(self.bot.db.histories()[0]['entries']), 1)
        self.assertIsNone(self.message.edit.call_args.kwargs['view'])

    async def test_partial_role_change_is_not_logged_until_completed(self):
        application_id = self.accepted()
        self.member.remove_roles.side_effect = self.denied()
        await self.bot.process(application_id)
        await self.bot.recover()
        self.assertEqual(self.bot.db.histories(), [])
        self.bot.log_channel.send.assert_not_awaited()
        self.assertEqual(self.user.send.await_count, 1)
        self.bot.db = Database(self.bot.db.path)
        self.member.remove_roles.side_effect = self.remove_role
        await self.bot.process(application_id)
        await self.bot.recover()
        self.bot.log_channel.send.assert_awaited_once()
        self.assertEqual(self.user.send.await_count, 2)
        self.member.add_roles.assert_awaited_once()

    async def test_unchanged_roles_and_single_role_change_do_not_log(self):
        self.member.roles = [MagicMock(id=self.bot.ids['MEMBER_ROLE_ID'])]
        application_id = self.accepted()
        await self.bot.process(application_id)
        await self.bot.recover()
        self.assertEqual(self.bot.db.histories(), [])
        self.bot.log_channel.send.assert_not_awaited()
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()

    async def test_only_member_added_without_progress_removed_does_not_log(self):
        self.member.roles = []
        application_id = self.accepted()
        await self.bot.process(application_id)
        self.assertTrue(self.bot.db.get(application_id)['roles_done'])
        self.assertEqual(self.bot.db.histories(), [])

    async def test_http_success_without_confirmed_roles_does_not_complete(self):
        self.member.add_roles.side_effect = None
        self.member.remove_roles.side_effect = None
        application_id = self.accepted()
        await self.bot.process(application_id)
        self.assertFalse(self.bot.db.get(application_id)['roles_done'])
        self.assertEqual(self.bot.db.histories(), [])

    async def test_rejection_does_not_change_roles_or_create_log(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.decide(row['id'], 3, 'rejected', 'Wrong name')
        await self.bot.process(row['id'])
        await self.bot.recover()
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.bot.log_channel.send.assert_not_awaited()
        self.assertIn('Wrong name', self.user.send.call_args.kwargs['embed'].description)

    async def test_closed_dms_do_not_block_roles_or_log(self):
        self.user.send.side_effect = self.denied()
        application_id = self.accepted()
        await self.bot.process(application_id)
        await self.bot.recover()
        self.assertTrue(self.bot.db.get(application_id)['roles_done'])
        self.assertEqual(self.bot.db.get(application_id)['decision_dm'], 'blocked')
        self.bot.log_channel.send.assert_awaited_once()

    async def test_admin_can_accept_own_application(self):
        row = self.bot.db.create(1, 2, 'Admin')
        self.bot.db.update(row['id'], message_id=50)
        self.member.guild_permissions.administrator = True
        interaction = self.interaction()
        interaction.user.guild_permissions.administrator = True
        await ReviewView(self.bot).accept.callback(interaction)
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'accepted')

    async def test_unprivileged_user_cannot_accept_even_with_modal(self):
        row = self.bot.db.create(1, 2, 'Player')
        await self.bot.decide(self.interaction(), row['id'], 'accepted')
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'pending')

    async def test_two_staff_decisions_only_first_wins(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50)
        self.member.guild_permissions.administrator = True
        await asyncio.gather(self.bot.decide(self.interaction(), row['id'], 'accepted'),
                             self.bot.decide(self.interaction(), row['id'], 'rejected', 'Too late'))
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'accepted')

    async def test_startup_recovery_does_not_assign_roles_to_everyone(self):
        await self.bot.recover()
        self.bot.guild.fetch_member.assert_not_awaited()
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.bot.log_channel.send.assert_not_awaited()

    async def test_member_role_is_preserved_on_join_and_submission(self):
        self.member.roles = [MagicMock(id=self.bot.ids['MEMBER_ROLE_ID'])]
        await self.bot.on_member_join(self.member)
        await self.bot.submit(self.interaction(public=True), 'Player')
        self.member.remove_roles.assert_not_awaited()
        self.member.add_roles.assert_not_awaited()
        self.assertEqual(self.bot.db.applications(), [])

    async def test_new_join_gets_progress_without_log(self):
        self.member.roles = []
        await self.bot.on_member_join(self.member)
        self.assertIn(self.bot.ids['PROGRESS_ROLE_ID'], self.role_ids())
        await self.bot.recover()
        self.bot.log_channel.send.assert_not_awaited()

    async def test_verified_rejoin_restores_member_without_log(self):
        application_id = self.accepted()
        self.bot.db.finish_acceptance(application_id)
        self.member.roles = []
        await self.bot.on_member_join(self.member)
        self.assertIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['PROGRESS_ROLE_ID'], self.role_ids())
        self.assertEqual(self.bot.db.histories(), [])

    async def test_deleted_completed_review_stays_deleted_after_restart(self):
        application_id = self.accepted()
        await self.bot.process(application_id)
        self.bot.review_channel.send.reset_mock()
        await self.bot.delete_messages(1, {50})
        self.bot.db = Database(self.bot.db.path)
        await self.bot.process(application_id)
        self.bot.review_channel.send.assert_not_awaited()
        self.assertTrue(self.bot.db.get(application_id)['review_deleted'])

    async def test_review_deleted_while_offline_is_not_recreated(self):
        application_id = self.accepted()
        self.bot.db.update(application_id, message_id=50)
        self.bot.review_channel.fetch_message.side_effect = self.missing()
        await self.bot.process(application_id)
        self.assertTrue(self.bot.db.get(application_id)['review_deleted'])
        self.assertTrue(self.bot.db.get(application_id)['roles_done'])
        self.bot.review_channel.send.assert_not_awaited()

    async def test_pending_deleted_review_is_cancelled_and_not_recreated(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50)
        self.bot.review_channel.fetch_message.side_effect = self.missing()
        await self.bot.process(row['id'])
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'cancelled')
        self.bot.review_channel.send.assert_not_awaited()
        self.assertIsNotNone(self.bot.db.create(1, 2, 'Player'))

    async def test_bulk_delete_marks_all_review_messages(self):
        first = self.bot.db.create(1, 2, 'A')
        second = self.bot.db.create(1, 3, 'B')
        self.bot.db.update(first['id'], message_id=50)
        self.bot.db.update(second['id'], message_id=51)
        await self.bot.on_raw_bulk_message_delete(MagicMock(guild_id=1, message_ids={50, 51}))
        self.assertTrue(all(r['review_deleted'] for r in self.bot.db.applications()))

    async def test_deleted_review_blocks_old_rejection_modal(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50)
        self.member.guild_permissions.administrator = True
        self.bot.review_channel.fetch_message.side_effect = self.missing()
        await self.bot.decide(self.interaction(), row['id'], 'rejected', 'Old form')
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'cancelled')

    async def test_existing_history_is_edited_not_reposted(self):
        application_id = self.accepted()
        await self.bot.process(application_id)
        history = self.bot.db.histories()[0]
        self.bot.db.update_history(1, 2, message_id=60)
        await self.bot.sync_history(self.bot.db.histories()[0])
        self.bot.log_channel.send.assert_not_awaited()
        self.log_message.edit.assert_awaited_once()
        self.assertEqual(len(history['entries']), 1)

    async def test_deleted_history_is_not_reposted(self):
        application_id = self.accepted()
        await self.bot.process(application_id)
        self.bot.db.update_history(1, 2, message_id=60)
        self.bot.log_channel.fetch_message.side_effect = self.missing()
        await self.bot.sync_history(self.bot.db.histories()[0])
        self.bot.log_channel.send.assert_not_awaited()
        self.assertTrue(self.bot.db.histories()[0]['deleted'])

    async def test_clean_review_has_no_unused_fields_or_completed_buttons(self):
        application_id = self.accepted()
        await self.bot.process(application_id)
        result = self.bot.review_embed(self.bot.db.get(application_id))
        names = {f.name for f in result.fields}
        self.assertFalse(names & {'Language', 'Rules agreed', 'Patrol availability', 'Experience', 'Claimed by', 'Reason / note'})
        self.assertIsNone(self.message.edit.call_args.kwargs['view'])

    async def test_unrecorded_review_send_is_recovered_without_duplicate(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot._connection.user = MagicMock(id=99)
        self.message.author.id = 99
        saved = discord.Embed(title='Pending')
        saved.set_footer(text=f"Application {row['id']}")
        self.message.embeds = [saved]
        async def history(**kwargs):
            yield self.message
        self.bot.review_channel.history = history
        await self.bot.sync_review(row['id'])
        self.bot.review_channel.send.assert_not_awaited()
        self.assertEqual(self.bot.db.get(row['id'])['message_id'], 50)

    async def test_unrecorded_history_send_is_recovered_without_duplicate(self):
        application_id = self.accepted()
        await self.bot.process(application_id)
        self.bot._connection.user = MagicMock(id=99)
        self.log_message.author.id = 99
        self.log_message.embeds = [self.bot.history_embed(self.bot.db.histories()[0])]
        async def history(**kwargs):
            yield self.log_message
        self.bot.log_channel.history = history
        await self.bot.sync_history(self.bot.db.histories()[0])
        self.bot.log_channel.send.assert_not_awaited()
        self.assertEqual(self.bot.db.histories()[0]['message_id'], 60)

    async def test_history_delivery_failure_retries_one_embed(self):
        application_id = self.accepted()
        await self.bot.process(application_id)
        self.bot.log_channel.send.side_effect = self.denied()
        await self.bot.recover()
        self.assertTrue(self.bot.db.histories()[0]['dirty'])
        self.bot.log_channel.send.side_effect = None
        self.bot.log_channel.send.reset_mock()
        await self.bot.recover()
        await self.bot.recover()
        self.bot.log_channel.send.assert_awaited_once()

    async def test_deleted_pending_does_not_send_late_submission_dm(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50)
        await self.bot.delete_messages(1, {50})
        await self.bot.process(row['id'])
        self.user.send.assert_awaited_once()
        self.assertIn('removed by staff', self.user.send.call_args.kwargs['embed'].description)

    async def test_accepted_rejoin_finishes_pending_transfer_before_restoration(self):
        application_id = self.accepted()
        await self.bot.on_member_join(self.member)
        self.assertTrue(self.bot.db.get(application_id)['roles_done'])
        self.assertEqual(len(self.bot.db.histories()), 1)

    async def test_allies_acceptance_nickname_and_history_survive_restart(self):
        row = self.bot.db.create(1, 2, 'Ally Player', 'allies')
        self.bot.db.decide(row['id'], 3, 'accepted')
        self.bot.db = Database(self.bot.db.path)
        await self.bot.process(row['id'])
        await self.bot.recover()
        self.assertIn(self.bot.ids['ALLIES_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['PROGRESS_ROLE_ID'], self.role_ids())
        self.assertEqual(self.member.nick, 'Ally Player')
        self.member.edit.assert_awaited_once()
        self.assertIn('Allies added', str(self.bot.history_embed(self.bot.db.histories()[0]).to_dict()))
        self.member.roles = []
        await self.bot.on_member_join(self.member)
        self.assertIn(self.bot.ids['ALLIES_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())

    async def test_nickname_forbidden_does_not_block_acceptance_or_retry_forever(self):
        self.member.edit.side_effect = self.denied()
        identifier = self.accepted()
        await self.bot.process(identifier)
        await self.bot.recover()
        self.member.edit.assert_awaited_once()
        row = self.bot.db.get(identifier)
        self.assertTrue(row['roles_done'])
        self.assertEqual(row['nickname_state'], 'blocked')
        self.assertEqual(row['decision_dm'], 'done')
        self.assertIn('Could not change nickname', str(self.bot.review_embed(row).to_dict()))

    async def test_long_game_name_kept_and_nickname_limited(self):
        row = self.bot.db.create(1, 2, 'A' * 100)
        self.bot.db.decide(row['id'], 3, 'accepted')
        await self.bot.process(row['id'])
        self.assertEqual(self.member.nick, 'A' * 32)
        self.assertEqual(self.bot.db.get(row['id'])['username'], 'A' * 100)

    async def test_nickname_transient_failure_retries_without_repeating_roles(self):
        identifier = self.accepted()
        original = self.member.edit.side_effect
        self.member.edit.side_effect = discord.HTTPException(MagicMock(status=503, reason='Unavailable'), 'Retry')
        await self.bot.process(identifier)
        self.assertEqual(self.bot.db.get(identifier)['nickname_state'], 'pending')
        self.member.edit.side_effect = original
        await self.bot.process(identifier)
        self.assertEqual(self.member.nick, 'Player')
        self.member.add_roles.assert_awaited_once()

    async def test_both_buttons_open_correct_modal(self):
        for button, kind in ((VerifyView(self.bot).verify, 'member'), (VerifyView(self.bot).allies, 'allies')):
            interaction = self.interaction(public=True)
            await button.callback(interaction)
            self.assertEqual(interaction.response.send_modal.call_args.args[0].kind, kind)

    async def test_cooldown_blocks_both_button_and_open_modal_submission(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.decide(row['id'], 3, 'rejected', 'Check name')
        self.bot.db = Database(self.bot.db.path)
        for button in (VerifyView(self.bot).verify, VerifyView(self.bot).allies):
            interaction = self.interaction(public=True)
            await button.callback(interaction)
            interaction.response.send_modal.assert_not_awaited()
        await self.bot.submit(self.interaction(public=True), 'Another name', 'allies')
        self.assertEqual(len(self.bot.db.applications()), 1)
        self.member.add_roles.assert_not_awaited()

    async def test_existing_allies_role_is_preserved_without_member_role(self):
        self.member.roles = [MagicMock(id=self.bot.ids['ALLIES_ROLE_ID'])]
        await self.bot.on_member_join(self.member)
        await self.bot.submit(self.interaction(public=True), 'Player', 'member')
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.assertEqual(self.bot.db.applications(), [])

    async def test_duplicate_warning_refreshes_existing_pending_review(self):
        other = self.bot.db.create(1, 3, 'player', 'allies')
        await self.bot.sync_review(other['id'])
        identifier = self.accepted()
        await self.bot.process(identifier)
        self.assertTrue(self.bot.db.get(other['id'])['review_dirty'])
        result = self.bot.review_embed(self.bot.db.get(other['id']))
        self.assertIn('DUPLICATE USERNAME', [f.name for f in result.fields])
        self.assertEqual(self.bot.db.get(other['id'])['status'], 'pending')
        self.assertLess(len(result), 6000)
