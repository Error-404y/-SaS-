import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from event_center import (
    DEFAULT_EVENT_BOARD_CHANNEL_ID,
    DEFAULT_EVENT_CONTROL_CHANNEL_ID,
    DEFAULT_EVENT_CONTROL_GUILD_ID,
    DEFAULT_EVENT_TARGET_GUILD_ID,
    DEFAULT_EVENT_TEXT_CHANNEL_ID,
    EventCenter,
    EventStore,
)
from event_controls import (
    CancelConfirmView,
    CreateEventModal,
    EditEventModal,
    EventControlView,
    EventPickerView,
    PostponeEventModal,
    open_event_action,
    selected_event,
)


class CrossServerEventPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'events.sqlite3'
        self.target = MagicMock()
        self.target.id = DEFAULT_EVENT_TARGET_GUILD_ID
        self.control = MagicMock()
        self.control.id = DEFAULT_EVENT_CONTROL_GUILD_ID
        self.control.me = MagicMock()
        self.bot = MagicMock()
        self.bot.user.id = 777
        self.bot.configured = True
        self.bot.guild = self.target
        self.bot.get_guild.side_effect = lambda gid: {
            self.target.id: self.target, self.control.id: self.control
        }.get(gid)
        self.channels = {}
        self.messages = {}
        self.sent = []
        for cid, guild in (
            (DEFAULT_EVENT_BOARD_CHANNEL_ID, self.target),
            (DEFAULT_EVENT_TEXT_CHANNEL_ID, self.target),
            (DEFAULT_EVENT_CONTROL_CHANNEL_ID, self.control),
        ):
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = cid
            channel.guild = guild
            channel.mention = f'<#{cid}>'
            permission = channel.permissions_for.return_value
            permission.view_channel = True
            permission.send_messages = True
            permission.embed_links = True
            permission.attach_files = True
            permission.read_message_history = True

            async def send_message(_cid=cid, **kwargs):
                msg = MagicMock()
                msg.id = 900 + len(self.sent)
                msg.jump_url = f'https://discord.com/channels/1/{_cid}/{msg.id}'
                msg.author.id = self.bot.user.id
                msg.embeds = [kwargs['embed']]
                msg.edit = AsyncMock(return_value=msg)
                msg.delete = AsyncMock()
                self.sent.append((_cid, kwargs))
                self.messages[msg.id] = msg
                return msg

            async def fetch_message(mid):
                if mid not in self.messages:
                    raise discord.NotFound(MagicMock(status=404, reason='Missing'), 'Missing')
                return self.messages[mid]

            async def history(*args, **kwargs):
                for msg in reversed(tuple(self.messages.values())):
                    if int(msg.jump_url.split('/')[-2]) == cid:
                        yield msg

            channel.send = AsyncMock(side_effect=send_message)
            channel.fetch_message = AsyncMock(side_effect=fetch_message)
            channel.history = history
            self.channels[cid] = channel
        self.target.get_channel.side_effect = self.channels.get
        self.control.get_channel.side_effect = self.channels.get
        self.bot.get_channel.side_effect = self.channels.get
        self.bot.fetch_channel = AsyncMock(side_effect=lambda cid: self.channels[cid])
        self.manager = EventCenter(self.bot, self.path)
        self.manager.bootstrap()

    def tearDown(self):
        self.manager.close()
        self.folder.cleanup()

    def interaction(self, *, guild_id=DEFAULT_EVENT_CONTROL_GUILD_ID,
                    channel_id=DEFAULT_EVENT_CONTROL_CHANNEL_ID,
                    administrator=True, roles=None, user_id=83):
        interaction = MagicMock()
        interaction.guild_id = guild_id
        interaction.channel_id = channel_id
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = user_id
        interaction.user.roles = roles or []
        interaction.user.guild_permissions.administrator = administrator
        interaction.response.send_modal = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    def schedule(self, title='Oilfield Strike', start_in=7200):
        now = int(datetime.now(timezone.utc).timestamp())
        event_id = self.manager.store.create(
            self.target.id, DEFAULT_EVENT_TEXT_CHANNEL_ID, title,
            'Alliance', 'Staff details', now+start_in,
            83, now=now, ends_at=now+start_in+3600
        )
        return event_id

    async def test_command_panel_is_posted_only_in_control_guild_and_reused_after_restart(self):
        first = await self.manager.ensure_management_panel()
        self.assertEqual(first.embeds[0].title, 'SaS | Event Command Center')
        self.assertEqual([cid for cid, _ in self.sent], [DEFAULT_EVENT_CONTROL_CHANNEL_ID])
        self.assertEqual(self.manager.store.control_panel(self.control.id)['message_id'], first.id)
        second = await self.manager.ensure_management_panel()
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.sent), 1)
        first.edit.assert_awaited_once()
        self.manager.close()
        self.manager = EventCenter(self.bot, self.path)
        self.manager.bootstrap()
        again = await self.manager.ensure_management_panel()
        self.assertEqual(again.id, first.id)
        self.assertEqual(len(self.sent), 1)

    async def test_only_control_server_officers_can_interact(self):
        allowed = self.interaction()
        self.assertTrue(self.manager.is_staff(allowed))
        self.assertFalse(self.manager.is_staff(self.interaction(guild_id=self.target.id)))
        self.assertFalse(self.manager.is_staff(self.interaction(channel_id=DEFAULT_EVENT_TEXT_CHANNEL_ID)))
        self.assertFalse(self.manager.is_staff(self.interaction(administrator=False)))
        self.manager.control_role_ids.add(5467)
        role_member = self.interaction(administrator=False, roles=[MagicMock(id=5467)])
        self.assertTrue(self.manager.is_staff(role_member))
        view = EventControlView(self.manager)
        blocked = self.interaction(administrator=False)
        self.assertFalse(await view.interaction_check(blocked))
        blocked.response.send_message.assert_awaited_once()

    async def test_create_button_opens_modal_and_writes_only_to_sas_channels(self):
        control = EventControlView(self.manager)
        interaction = self.interaction()
        await control.create.callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, CreateEventModal)
        modal.event_title._value = 'Heroes Unite'
        modal.start_time._value = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime('%Y-%m-%d %H:%M:%S')
        modal.end_time._value = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
        modal.category._value = 'Alliance'
        modal.details._value = 'Join in-game'
        await modal.on_submit(interaction)
        row = self.manager.store.manage_events(self.target.id)[0]
        self.assertEqual(row['title'], 'Heroes Unite')
        self.assertEqual(row['guild_id'], self.target.id)
        self.assertEqual(
            [cid for cid, _ in self.sent],
            [DEFAULT_EVENT_TEXT_CHANNEL_ID, DEFAULT_EVENT_BOARD_CHANNEL_ID,
             DEFAULT_EVENT_CONTROL_CHANNEL_ID]
        )
        self.assertEqual(self.manager.store.board_event_count(self.control.id), 0)
        self.assertIsNotNone(self.manager.store.settings(self.target.id)['text_message_id'])
        self.assertIsNotNone(self.manager.store.settings(self.target.id)['calendar_message_id'])

    async def test_edit_postpone_and_cancel_update_target_records(self):
        event_id = self.schedule()
        interaction = self.interaction()
        await open_event_action(self.manager, interaction, event_id, 'edit')
        form = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(form, EditEventModal)
        form.event_title._value = 'Oilfield Strike II'
        form.details._value = 'New instructions'
        await form.on_submit(interaction)
        self.assertEqual(self.manager.store.event(event_id)['title'], 'Oilfield Strike II')
        self.assertEqual(self.manager.store.event(event_id)['notes'], 'New instructions')
        interaction.response.send_modal.reset_mock()
        await open_event_action(self.manager, interaction, event_id, 'postpone')
        postpone = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(postpone, PostponeEventModal)
        postpone.new_start._value = (datetime.now(timezone.utc) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        original_duration = self.manager.store.event(event_id)['ends_at'] - self.manager.store.event(event_id)['starts_at']
        await postpone.on_submit(interaction)
        latest = self.manager.store.event(event_id)
        self.assertEqual(latest['ends_at'] - latest['starts_at'], original_duration)
        await open_event_action(self.manager, interaction, event_id, 'cancel')
        pending = interaction.response.send_message.await_args.kwargs['view']
        self.assertIsInstance(pending, CancelConfirmView)
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()
        await pending.confirm.callback(interaction)
        self.assertEqual(self.manager.store.event(event_id)['state'], 'cancelled')
        self.assertEqual(self.manager.store.board_event_count(self.target.id), 0)

    async def test_dropdown_and_numeric_lookup_support_event_selection(self):
        for n in range(30):
            self.schedule(f'Event {n:02d}')
        records = self.manager.store.manage_events(self.target.id)
        self.assertEqual(len(records), 25)
        view = EventPickerView(self.manager, 'inspect', records, user_id=83)
        self.assertEqual(len(next(item for item in view.children if isinstance(item, discord.ui.Select)).options), 25)
        self.assertEqual(next(item.label for item in view.children if isinstance(item, discord.ui.Button)), 'Enter Event ID')
        self.assertIsNotNone(selected_event(self.manager, records[24]['id']))
        self.assertIsNotNone(selected_event(self.manager, records[0]['id'] + 29))
        bad = self.manager.store.create(
            self.control.id, DEFAULT_EVENT_CONTROL_CHANNEL_ID,
            'Not SaS', 'Alliance', '', int(datetime.now(timezone.utc).timestamp())+7200,
            99, ends_at=int(datetime.now(timezone.utc).timestamp())+9000
        )
        self.assertIsNone(selected_event(self.manager, bad))

    async def test_existing_event_and_reminder_tables_survive_panel_upgrade(self):
        eid = self.schedule()
        reminders = self.manager.store.conn.execute(
            "SELECT stage FROM event_reminders WHERE event_id=?", (eid,)
        ).fetchall()
        self.assertEqual([r['stage'] for r in reminders], ['1h'])
        await self.manager.ensure_management_panel()
        self.manager.close()
        self.manager = EventCenter(self.bot, self.path)
        self.manager.bootstrap()
        self.assertEqual(self.manager.store.event(eid)['title'], 'Oilfield Strike')
        self.assertEqual(self.manager.store.conn.execute(
            "SELECT stage FROM event_reminders WHERE event_id=?", (eid,)
        ).fetchone()['stage'], '1h')


if __name__ == '__main__':
    unittest.main()
