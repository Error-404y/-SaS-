import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.components import _component_factory

from database import Database
from test_workflow import BotFixture
from views import PauseControls, ReviewView, VerifyView, embed


class ControlCenterTests(BotFixture, unittest.IsolatedAsyncioTestCase):
    def matching_message(self, result, view=None):
        message = MagicMock(id=80)
        message.channel.id = 100
        message.embeds = [discord.Embed.from_dict(result.to_dict())]
        message.components = [_component_factory(row) for row in view.to_components()] if view else []
        message.edit = AsyncMock()
        return message

    async def test_unchanged_review_skips_patch_even_when_dirty(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=80)
        message = self.matching_message(self.bot.review_embed(row), ReviewView(self.bot))
        self.bot.review_channel.fetch_message.return_value = message
        await self.bot.sync_review(row['id'])
        message.edit.assert_not_awaited()
        self.assertFalse(self.bot.db.get(row['id'])['review_dirty'])

    async def test_embed_proxy_metadata_does_not_trigger_edit(self):
        result = embed('Panel')
        result.set_thumbnail(url='https://example.com/icon.png')
        result.set_author(name='SaS', icon_url='https://example.com/icon.png')
        message = self.matching_message(result)
        returned = result.to_dict()
        returned['thumbnail'].update(proxy_url='https://example.com/proxy', height=64, width=64, flags=0)
        returned['author']['proxy_icon_url'] = 'https://example.com/proxy'
        message.embeds = [discord.Embed.from_dict(returned)]
        self.assertFalse(await self.bot.edit_message(message, result))
        message.edit.assert_not_awaited()

    async def test_changed_button_or_removed_management_button_is_edited(self):
        result = embed('Panel')
        previous = VerifyView(self.bot)
        previous.add_item(discord.ui.Button(label='Staff controls', custom_id='old:control', row=2))
        message = self.matching_message(result, previous)
        self.assertTrue(await self.bot.edit_message(message, result, VerifyView(self.bot)))
        message.edit.assert_awaited_once()

    async def test_completed_review_removes_buttons_even_when_embed_matches(self):
        result = embed('Accepted')
        message = self.matching_message(result, ReviewView(self.bot))
        await self.bot.edit_message(message, result)
        self.assertIsNone(message.edit.call_args.kwargs['view'])

    async def test_approval_updates_review_only_once_with_final_dm_status(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=50, submission_dm='done', review_dirty=False)
        self.bot.db.decide(row['id'], 3, 'accepted')
        await self.bot.process(row['id'])
        self.message.edit.assert_awaited_once()
        fields = {field.name: field.value for field in self.message.edit.call_args.kwargs['embed'].fields}
        self.assertEqual(fields['RESULT DM'], 'Sent successfully')
        self.assertEqual(fields['ACCESS'], 'Role update confirmed.')

    async def test_startup_does_not_dirty_historical_reviews(self):
        identifier = self.accepted()
        await self.bot.process(identifier)
        self.assertFalse(self.bot.db.get(identifier)['review_dirty'])
        self.bot.configured = False
        self.bot.validate = AsyncMock()
        self.bot.validate_log_channel = AsyncMock()
        self.bot.validate_control_channel = AsyncMock()
        self.bot.ensure_panel = AsyncMock()
        self.bot.ensure_control_panel = AsyncMock()
        self.bot.discover_member_prefixes = AsyncMock()
        tree = MagicMock()
        tree.fetch_commands = AsyncMock(return_value=[])
        with patch('bot.app_commands.CommandTree', return_value=tree), patch.object(self.bot.recover, 'start'):
            await self.bot.on_ready()
            await asyncio.sleep(0)
        self.assertFalse(self.bot.db.get(identifier)['review_dirty'])

    async def test_control_panel_survives_restart_without_duplicate_post(self):
        await self.bot.ensure_control_panel()
        self.bot.control_channel.send.assert_awaited_once()
        self.bot.db = Database(self.bot.db.path)
        message = self.matching_message(self.bot.control_embed(), PauseControls(self.bot))
        message.id = 70
        self.bot.control_channel.fetch_message.return_value = message
        await self.bot.ensure_control_panel()
        self.bot.control_channel.send.assert_awaited_once()
        message.edit.assert_not_awaited()
        self.assertTrue(PauseControls(self.bot).is_persistent())

    async def test_control_panel_recovery_finds_unrecorded_post(self):
        self.bot._connection.user = MagicMock(id=99)
        self.control_message.author.id = 99
        self.control_message.embeds = [self.bot.control_embed()]
        self.control_message.components = [_component_factory(row) for row in PauseControls(self.bot).to_components()]
        async def history(**kwargs):
            yield self.control_message
        self.bot.control_channel.history = history
        await self.bot.ensure_control_panel()
        self.bot.control_channel.send.assert_not_awaited()
        self.assertEqual(self.bot.db.control_panel()['message_id'], 70)

    async def test_pause_rejected_from_old_public_controls_and_admin_without_role(self):
        self.member.roles.append(MagicMock(id=self.bot.ids['STAFF_ROLE_ID']))
        await self.bot.set_application_pause(self.interaction(public=True), True, 'Closed')
        self.assertFalse(self.bot.db.paused()['active'])
        self.member.roles = []
        self.member.guild_permissions.administrator = True
        await self.bot.set_application_pause(self.interaction(control=True), True, 'Closed')
        self.assertFalse(self.bot.db.paused()['active'])

    async def test_pause_button_checks_channel_and_role_before_opening_modal(self):
        interaction = self.interaction(control=True)
        await PauseControls(self.bot).pause.callback(interaction)
        interaction.response.send_modal.assert_not_awaited()
        interaction.user.roles = [MagicMock(id=self.bot.ids['STAFF_ROLE_ID'])]
        await PauseControls(self.bot).pause.callback(interaction)
        interaction.response.send_modal.assert_awaited_once()

    async def test_control_panel_failure_retries_independently_of_public_panel(self):
        self.member.roles.append(MagicMock(id=self.bot.ids['STAFF_ROLE_ID']))
        self.bot.verify_channel = MagicMock()
        self.bot.verify_channel.send = AsyncMock(return_value=self.message)
        self.bot.control_channel.send.side_effect = self.denied()
        await self.bot.set_application_pause(self.interaction(control=True), True, 'Closed')
        self.assertFalse(self.bot.db.paused()['dirty'])
        self.assertTrue(self.bot.db.paused()['control_dirty'])
        self.bot.control_channel.send.side_effect = None
        await self.bot.recover()
        self.assertFalse(self.bot.db.paused()['control_dirty'])
        self.bot.verify_channel.send.assert_awaited_once()

    async def test_edits_wait_for_channel_spacing(self):
        message = self.matching_message(embed('Before'))
        del self.bot.wait_for_edit
        with patch('bot.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.bot.edit_message(message, embed('After'))
            sleep.assert_not_awaited()
            await self.bot.edit_message(message, embed('Another update'))
            sleep.assert_awaited_once()
            self.assertGreater(sleep.call_args.args[0], 0)
            self.assertLessEqual(sleep.call_args.args[0], 1.2)

    async def test_setup_registers_persistent_staff_controls(self):
        with patch.object(self.bot, 'add_view') as add_view:
            await self.bot.setup_hook()
        views = [call.args[0] for call in add_view.call_args_list]
        self.assertTrue(any(isinstance(view, PauseControls) and view.is_persistent() for view in views))

    async def test_repeated_pause_setting_does_not_rewrite_panels(self):
        self.member.roles.append(MagicMock(id=self.bot.ids['STAFF_ROLE_ID']))
        self.bot.db.set_pause(True, 'Closed', 3)
        before = self.bot.db.paused()
        self.bot.ensure_panel = AsyncMock()
        self.bot.ensure_control_panel = AsyncMock()
        await self.bot.set_application_pause(self.interaction(control=True), True, 'Closed')
        self.bot.ensure_panel.assert_not_awaited()
        self.bot.ensure_control_panel.assert_not_awaited()
        self.assertEqual(self.bot.db.paused(), before)
