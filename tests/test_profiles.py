import unittest
from unittest.mock import AsyncMock, MagicMock


from database import Database
from identity import nickname
from test_workflow import BotFixture
from views import ApplicationModal, ClanChoiceView, VerifyView, ClanChangeModal


class ProfileTests(BotFixture, unittest.IsolatedAsyncioTestCase):
    def verified(self, kind='member', clan='SaS', username='Player'):
        profile = dict(kind=kind, clan=clan, username=username)
        self.bot.db.save_profile(1, 2, profile)
        self.member.roles = [MagicMock(id=self.bot.target_role(profile))]
        self.member.nick = nickname(profile)
        return profile

    async def approve_latest(self):
        row = self.bot.db.applications()[-1]
        self.bot.db.decide(row['id'], 3, 'accepted')
        await self.bot.process(row['id'])
        return self.bot.db.get(row['id'])

    async def test_allies_form_requires_username_and_clan(self):
        modal = ApplicationModal(self.bot, 'allies')
        self.assertEqual([item.label for item in modal.children], ['Police Simulator username', 'Clan name'])
        self.assertTrue(modal.clan.required)
        await self.bot.submit(self.interaction(public=True), 'hqayoo', 'allies', 'SaS')
        row = await self.approve_latest()
        self.assertEqual(self.member.nick, '[SaS] hqayoo')
        self.assertIn(self.bot.ids['ALLIES_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())
        self.assertEqual(row['clan'], 'SaS')

    async def test_name_change_preserves_clan_and_roles_until_approved(self):
        previous = self.verified('allies', 'ABC')
        await self.bot.submit_change(self.interaction(public=True), 'name', 'hqayoo')
        self.assertEqual(self.bot.db.profile(1, 2), previous)
        self.assertEqual(self.member.nick, '[ABC] Player')
        self.member.edit.assert_not_awaited()
        row = await self.approve_latest()
        self.assertEqual(self.member.nick, '[ABC] hqayoo')
        self.assertEqual(self.bot.db.profile(1, 2)['username'], 'hqayoo')
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.assertEqual(row['operation'], 'name')
        self.assertEqual(self.bot.db.histories(), [])

    async def test_rejected_change_preserves_profile_nickname_and_access(self):
        previous = self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        row = self.bot.db.applications()[-1]
        self.bot.db.decide(row['id'], 3, 'rejected', 'Wrong username')
        await self.bot.process(row['id'])
        self.assertEqual(self.bot.db.profile(1, 2), previous)
        self.member.edit.assert_not_awaited()
        self.assertEqual(self.member.nick, '[SaS] Player')
        self.member.remove_roles.assert_not_awaited()

    async def test_join_sas_is_member_review_and_switches_roles_only_on_acceptance(self):
        self.verified('allies', 'ABC', 'hqayoo')
        await ClanChoiceView(self.bot).join_sas.callback(self.interaction(public=True))
        row = self.bot.db.applications()[-1]
        self.assertEqual(row['operation'], 'join_member')
        self.assertEqual(row['kind'], 'member')
        self.assertIn('Member verification', self.bot.review_embed(row).title)
        self.assertEqual(self.member.nick, '[ABC] hqayoo')
        self.member.add_roles.assert_not_awaited()
        await self.approve_latest()
        self.assertEqual(self.member.nick, '[SaS] hqayoo')
        self.assertIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['ALLIES_ROLE_ID'], self.role_ids())
        self.assertEqual(self.bot.db.histories(), [])

    async def test_member_leaving_for_other_clan_becomes_ally_after_approval(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'clan', 'XYZ')
        self.assertIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())
        await self.approve_latest()
        self.assertEqual(self.member.nick, '[XYZ] Player')
        self.assertIn(self.bot.ids['ALLIES_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())
        self.bot.db = Database(self.bot.db.path)
        self.member.roles = []
        await self.bot.on_member_join(self.member)
        self.assertIn(self.bot.ids['ALLIES_ROLE_ID'], self.role_ids())
        self.assertNotIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())

    async def test_other_clan_button_opens_clan_form(self):
        interaction = self.interaction(public=True)
        await ClanChoiceView(self.bot).other_clan.callback(interaction)
        self.assertIsInstance(interaction.response.send_modal.call_args.args[0], ClanChangeModal)

    async def test_unverified_users_cannot_submit_profile_changes(self):
        for operation in ('name', 'clan', 'join_member'):
            await self.bot.submit_change(self.interaction(public=True), operation, 'Other')
        self.assertEqual(self.bot.db.applications(), [])

    async def test_two_changes_cannot_overlap(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        await self.bot.submit_change(self.interaction(public=True), 'clan', 'Other')
        self.assertEqual(len(self.bot.db.applications()), 1)

    async def test_unfinished_approved_change_blocks_new_change_after_restart(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        row = self.bot.db.applications()[-1]
        self.bot.db.decide(row['id'], 3, 'accepted')
        self.bot.db = Database(self.bot.db.path)
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewerName')
        self.assertEqual(len(self.bot.db.applications()), 1)
        await self.bot.process(row['id'])
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewerName')
        self.assertEqual(len(self.bot.db.applications()), 2)

    async def test_old_accepted_request_never_overwrites_new_profile(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        first = await self.approve_latest()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewerName')
        await self.approve_latest()
        self.bot.db = Database(self.bot.db.path)
        await self.bot.process(first['id'])
        self.assertEqual(self.member.nick, '[SaS] NewerName')
        self.assertEqual(self.bot.db.profile(1, 2)['username'], 'NewerName')

    async def test_name_change_duplicate_warning_uses_current_name_only(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        await self.approve_latest()
        self.assertEqual(self.bot.db.duplicates(dict(guild_id=1, user_id=3, username='Player')), [])
        self.assertEqual(self.bot.db.duplicates(dict(guild_id=1, user_id=3, username='newname')), [2])

    async def test_success_and_failed_result_dm_indicators_are_persisted_and_edited(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        row = self.bot.db.applications()[-1]
        self.assertIn('Sent successfully', str(self.bot.review_embed(row).to_dict()))
        self.user.send.side_effect = self.denied()
        await self.approve_latest()
        result = self.message.edit.call_args.kwargs['embed']
        fields = {f.name: f.value for f in result.fields}
        self.assertEqual(fields['SUBMISSION DM'], 'Sent successfully')
        self.assertEqual(fields['RESULT DM'], 'Delivery failed / DMs unavailable')
        self.bot.db = Database(self.bot.db.path)
        await self.bot.process(row['id'])
        self.assertEqual(self.bot.db.get(row['id'])['decision_dm'], 'blocked')

    async def test_name_change_permission_failure_is_visible_without_false_success(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        self.member.edit.side_effect = self.denied()
        row = await self.approve_latest()
        self.assertEqual(row['nickname_state'], 'blocked')
        self.assertEqual(row['decision_dm'], 'done')
        self.assertIn('could not be applied', self.user.send.call_args.kwargs['embed'].description)
        self.assertEqual(self.member.nick, '[SaS] Player')

    async def test_partial_clan_role_switch_retries_after_restart(self):
        self.verified('allies', 'ABC')
        await self.bot.submit_change(self.interaction(public=True), 'join_member')
        self.member.remove_roles.side_effect = self.denied()
        row = await self.approve_latest()
        self.assertFalse(row['roles_done'])
        self.assertEqual(self.bot.db.profile(1, 2)['kind'], 'allies')
        self.assertEqual(self.member.nick, '[ABC] Player')
        self.bot.db = Database(self.bot.db.path)
        self.member.remove_roles.side_effect = self.remove_role
        await self.bot.process(row['id'])
        self.assertEqual(self.member.nick, '[SaS] Player')
        self.assertNotIn(self.bot.ids['ALLIES_ROLE_ID'], self.role_ids())
        self.assertEqual(self.bot.db.profile(1, 2)['kind'], 'member')

    async def test_pause_requires_actual_high_rank_role_even_for_admin(self):
        self.member.guild_permissions.administrator = True
        await self.bot.set_application_pause(self.interaction(public=True), True, 'Maintenance')
        self.assertFalse(self.bot.db.paused()['active'])
        self.member.roles.append(MagicMock(id=self.bot.ids['STAFF_ROLE_ID']))
        self.bot.ensure_panel = AsyncMock()
        await self.bot.set_application_pause(self.interaction(public=True), True, 'Maintenance')
        self.assertTrue(self.bot.db.paused()['active'])
        self.member.roles = []
        await self.bot.set_application_pause(self.interaction(public=True), False)
        self.assertTrue(self.bot.db.paused()['active'])

    async def test_pause_survives_restart_disables_buttons_and_blocks_open_form(self):
        self.bot.db.set_pause(True, 'Maintenance', 3)
        self.bot.db = Database(self.bot.db.path)
        view = VerifyView(self.bot)
        self.assertTrue(view.verify.disabled)
        self.assertTrue(view.allies.disabled)
        self.assertFalse(view.name_change.disabled)
        await self.bot.submit(self.interaction(public=True), 'Player', 'member')
        self.assertEqual(self.bot.db.applications(), [])
        self.assertIn('Maintenance', str(self.bot.panel_embed().to_dict()))

    async def test_pause_blocks_join_sas_but_allows_profile_maintenance(self):
        self.verified('allies', 'ABC')
        self.bot.db.set_pause(True, 'Maintenance', 3)
        await self.bot.submit_change(self.interaction(public=True), 'join_member')
        self.assertEqual(self.bot.db.applications(), [])
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        self.assertEqual(len(self.bot.db.applications()), 1)

    async def test_pause_panel_failure_retries_and_resume_reenables_buttons(self):
        self.member.roles.append(MagicMock(id=self.bot.ids['STAFF_ROLE_ID']))
        self.bot.verify_channel = MagicMock()
        self.bot.verify_channel.send = AsyncMock(side_effect=self.denied())
        await self.bot.set_application_pause(self.interaction(public=True), True, 'Maintenance')
        self.assertTrue(self.bot.db.paused()['dirty'])
        self.bot.verify_channel.send = AsyncMock(return_value=self.message)
        self.bot.verify_channel.fetch_message = AsyncMock(return_value=self.message)
        await self.bot.recover()
        self.assertFalse(self.bot.db.paused()['dirty'])
        await self.bot.set_application_pause(self.interaction(public=True), False)
        view = self.message.edit.call_args.kwargs['view']
        self.assertFalse(view.verify.disabled)
        self.assertFalse(view.allies.disabled)

    async def test_existing_member_prefix_once_no_role_changes_or_logs(self):
        self.member.roles = [MagicMock(id=self.bot.ids['MEMBER_ROLE_ID'])]
        self.member.nick = '[Other] hqayoo'
        self.bot.db.queue_nickname(1, 2)
        await self.bot.recover()
        self.assertEqual(self.member.nick, '[SaS] hqayoo')
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.bot.log_channel.send.assert_not_awaited()
        self.bot.db = Database(self.bot.db.path)
        self.bot.db.queue_nickname(1, 2)
        await self.bot.recover()
        self.member.edit.assert_awaited_once()

    async def test_member_prefix_does_not_duplicate_tag_or_touch_allies(self):
        self.verified()
        self.bot.db.queue_nickname(1, 2)
        await self.bot.recover()
        self.member.edit.assert_not_awaited()
        self.verified('allies', 'ABC')
        self.bot.db.queue_nickname(1, 2, force=True)
        await self.bot.recover()
        self.assertEqual(self.member.nick, '[ABC] Player')
        self.member.edit.assert_not_awaited()

    async def test_prefix_forbidden_does_not_spam_retries(self):
        self.verified()
        self.member.nick = 'Player'
        self.member.edit.side_effect = self.denied()
        self.bot.db.queue_nickname(1, 2)
        await self.bot.recover()
        await self.bot.recover()
        self.member.edit.assert_awaited_once()
        self.bot.log_channel.send.assert_not_awaited()

    async def test_invalid_or_oversized_clan_nicknames_are_not_silently_truncated(self):
        for username, clan in (('A' * 27, 'SaS'), ('[SaS] Player', 'SaS'), ('Player', '@everyone'), ('Player', '')):
            await self.bot.submit(self.interaction(public=True), username, 'allies', clan)
        self.assertEqual(self.bot.db.applications(), [])

    async def test_review_and_panel_fit_discord_embed_limits(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'clan', 'Longest Clan')
        row = self.bot.db.applications()[-1]
        for result in (self.bot.review_embed(row), self.bot.panel_embed()):
            self.assertLessEqual(len(result), 6000)
            self.assertTrue(all(len(f.value) <= 1024 for f in result.fields))
            self.assertLessEqual(len(result.fields), 25)

    async def test_deleted_name_change_is_not_recreated_or_applied(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        row = self.bot.db.applications()[-1]
        await self.bot.delete_messages(1, {50})
        self.bot.db = Database(self.bot.db.path)
        self.bot.review_channel.send.reset_mock()
        await self.bot.process(row['id'])
        self.bot.review_channel.send.assert_not_awaited()
        self.assertEqual(self.member.nick, '[SaS] Player')
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'cancelled')

    async def test_startup_discovers_only_members_and_saves_prefix_jobs(self):
        self.member.roles = [MagicMock(id=self.bot.ids['MEMBER_ROLE_ID'])]
        ally = MagicMock(id=3, bot=False, roles=[MagicMock(id=self.bot.ids['ALLIES_ROLE_ID'])])
        automated = MagicMock(id=4, bot=True, roles=[MagicMock(id=self.bot.ids['MEMBER_ROLE_ID'])])
        async def members(**kwargs):
            for member in (self.member, ally, automated):
                yield member
        self.bot.guild.fetch_members = members
        await self.bot.discover_member_prefixes()
        self.assertEqual([job['user_id'] for job in self.bot.db.nickname_jobs()], [2])
        self.bot.db = Database(self.bot.db.path)
        await self.bot.recover()
        self.assertEqual(self.member.nick, '[SaS] Player')
        self.member.add_roles.assert_not_awaited()

    async def test_later_member_role_grant_queues_prefix_but_nickname_edits_do_not(self):
        before = MagicMock(roles=[])
        self.member.roles = [MagicMock(id=self.bot.ids['MEMBER_ROLE_ID'])]
        await self.bot.on_member_update(before, self.member)
        await self.bot.recover()
        self.assertEqual(self.member.nick, '[SaS] Player')
        await self.bot.on_member_update(self.member, self.member)
        self.assertEqual(self.bot.db.nickname_jobs(), [])

    async def test_public_controls_serialize_in_separate_rows(self):
        components = VerifyView(self.bot).to_components()
        self.assertEqual([[button['label'] for button in row['components']] for row in components],
                         [['Verify - Member', 'Verify - Allies'], ['Name change', 'Clan change'], ['Staff controls']])
        identifiers = [button['custom_id'] for row in components for button in row['components']]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    async def test_member_prefix_does_not_repeat_saved_clan_tag(self):
        self.verified(username='[SaS] Player')
        self.member.nick = 'Player'
        self.bot.db.queue_nickname(1, 2)
        await self.bot.recover()
        self.assertEqual(self.member.nick, '[SaS] Player')

    async def test_approved_name_change_success_dm_and_staff_indicator(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        await self.approve_latest()
        fields = {f.name: f.value for f in self.message.edit.call_args.kwargs['embed'].fields}
        self.assertEqual(fields['RESULT DM'], 'Sent successfully')
        self.assertIn('NewName', self.user.send.call_args.kwargs['embed'].description)

    async def test_returning_member_during_approved_name_change_regains_access(self):
        self.verified()
        await self.bot.submit_change(self.interaction(public=True), 'name', 'NewName')
        row = self.bot.db.applications()[-1]
        self.bot.db.decide(row['id'], 3, 'accepted')
        self.member.roles = []
        await self.bot.on_member_join(self.member)
        await self.bot.process(row['id'])
        self.assertIn(self.bot.ids['MEMBER_ROLE_ID'], self.role_ids())
        self.assertEqual(self.member.nick, '[SaS] NewName')
