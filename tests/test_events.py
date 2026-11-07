import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path

from PIL import Image

from event_center import EventStore, EventCenter, CalendarNavigation, parse_utc, event_phase
from bot import VerificationBot
from unittest.mock import patch, MagicMock, AsyncMock
import os
import discord
from event_graphics import weekly_calendar, event_board


class EventStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.directory.name) / 'events.sqlite3')
        self.now = 1800000000

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def create(self, start_offset=90000):
        return self.store.create(1, 2, 'Alliance Training', 'Alliance', 'Prepare your team.', self.now + start_offset, 5, self.now)

    def test_only_one_hour_member_ping_is_scheduled(self):
        event_id = self.create()
        reminders = self.store.conn.execute(
            'SELECT stage,due_at FROM event_reminders WHERE event_id=?',
            (event_id,)
        ).fetchall()
        self.assertEqual([(row['stage'], row['due_at']) for row in reminders],
                         [('1h', self.now + 86400)])

    def test_no_member_ping_if_event_begins_less_than_one_hour_away(self):
        event_id = self.create(1800)
        stages = [row['stage'] for row in self.store.conn.execute(
            'SELECT stage FROM event_reminders WHERE event_id=?', (event_id,)
        )]
        self.assertEqual(stages, [])

    def test_member_ping_state_is_persistent_and_once_only(self):
        event_id = self.create()
        self.store.reminder_state(event_id, '1h', 'sent', 100)
        self.store.reminder_state(event_id, '1h', 'sent', 200)
        row = self.store.conn.execute(
            "SELECT state,message_id FROM event_reminders WHERE event_id=? AND stage='1h'",
            (event_id,)
        ).fetchone()
        self.assertEqual((row['state'],row['message_id']), ('sent',100))
        self.assertEqual(self.store.due(self.now + 86401), [])

    def test_cancel_stops_all_pending(self):
        event_id = self.create()
        self.assertTrue(self.store.cancel(event_id, 1))
        self.assertFalse(self.store.cancel(event_id, 1))
        states = {row['state'] for row in self.store.conn.execute('SELECT state FROM event_reminders')}
        self.assertEqual(states, {'skipped'})

    def test_long_events_are_visible_across_week_boundaries(self):
        event_id = self.store.create(
            1, 2, 'Alliance Mobilization', 'Alliance', 'Notes',
            self.now + 90000, 5, self.now,
            ends_at=self.now + 4 * 86400
        )
        beginning = self.now + 3 * 86400
        rows = self.store.calendar_events(1, beginning, beginning + 7 * 86400)
        self.assertIn(event_id, [row['id'] for row in rows])
        self.assertEqual(self.store.event(event_id)['ends_at'], self.now + 4 * 86400)

    def test_rescheduling_rebuilds_one_hour_member_alert(self):
        event_id = self.create()
        self.store.reminder_state(event_id, '1h', 'sent', 884)
        new_start = self.now + 100000
        self.assertTrue(self.store.edit(event_id, 1, 'Alliance Training', 'Alliance', 'Ready',
                                        new_start, new_start + 7200, now=self.now))
        self.assertEqual(self.store.event(event_id)['title'], 'Alliance Training')
        scheduled = self.store.conn.execute(
            'SELECT stage,state,due_at FROM event_reminders WHERE event_id=?',
            (event_id,)
        ).fetchall()
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]['stage'], '1h')
        self.assertTrue(all(row['state'] == 'pending' for row in scheduled))
        self.assertFalse(self.store.edit(event_id, 999, 'a', 'Other', '', new_start, new_start + 7200, now=self.now))

    def test_migration_from_old_events_without_end_times(self):
        original = self.create()
        self.store.conn.execute('UPDATE game_events SET ends_at=NULL WHERE id=?', (original,))
        self.store.close()
        self.store = EventStore(Path(self.directory.name) / 'events.sqlite3')
        row = self.store.event(original)
        self.assertEqual(row['ends_at'], row['starts_at'] + 3600)

    def test_public_calendar_board_persists_and_refreshes(self):
        self.store.set_channel(1, 88, None)
        self.store.set_board(1, 445, self.now)
        self.assertEqual(self.store.settings(1)['calendar_message_id'], 445)
        self.assertNotIn(1, [row['guild_id'] for row in self.store.boards_to_refresh(self.now + 200)])
        self.assertIn(1, [row['guild_id'] for row in self.store.boards_to_refresh(self.now + 3700)])
        self.store.close()
        self.store = EventStore(Path(self.directory.name) / 'events.sqlite3')
        self.assertEqual(self.store.settings(1)['calendar_message_id'], 445)
        self.store.set_channel(1, 99, None)
        self.assertEqual(self.store.settings(1)['calendar_message_id'], 445)
        self.assertEqual(self.store.settings(1)['calendar_message_channel_id'], 88)

    def test_new_events_share_one_board_with_different_titles(self):
        ongoing = self.store.create(1, 2, 'Port Inspection', 'Port Inspection', '',
                                    self.now - 3600, 7, now=self.now, ends_at=self.now + 86400)
        upcoming = self.store.create(1, 2, 'Work Log', 'Port Inspection', '',
                                     self.now + 7200, 7, now=self.now, ends_at=self.now + 172800)
        entries = self.store.board_events(1, now=self.now)
        self.assertEqual([row['title'] for row in entries], ['Port Inspection', 'Work Log'])
        self.assertEqual([row['id'] for row in entries], [ongoing, upcoming])
        self.assertEqual(self.store.board_event_count(1, now=self.now), 2)
        self.assertEqual(self.store.due(now=self.now), [])

    def test_event_phases_match_start_and_end_exactly(self):
        event_id = self.create(start_offset=90000)
        event = self.store.event(event_id)
        self.assertEqual(event_phase(event, self.now), 'Upcoming')
        self.assertEqual(event_phase(event, event['starts_at']), 'In Progress')
        self.assertEqual(event_phase(event, event['ends_at'] - 1), 'In Progress')
        self.assertEqual(event_phase(event, event['ends_at']), 'Completed')
        self.store.cancel(event_id, 1)
        self.assertEqual(event_phase(self.store.event(event_id), self.now), 'Cancelled')

    def test_status_refresh_tracks_existing_message_records(self):
        event_id = self.create(start_offset=1800)
        self.store.set_message(event_id, 852)
        self.assertEqual(self.store.event(event_id)['display_phase'], 'Upcoming')
        future = self.now + 3600
        pending = self.store.cards_to_refresh(now=future)
        self.assertIn(event_id, [row['id'] for row in pending])
        self.store.mark_display_phase(event_id, 'Completed')
        self.assertEqual(self.store.event(event_id)['display_phase'], 'Completed')
        self.store.mark_card_missing(event_id)
        self.assertIsNone(self.store.event(event_id)['message_id'])

    def test_settings_survive_restart(self):
        self.store.set_channel(1, 456, 987)
        self.store.close()
        self.store = EventStore(Path(self.directory.name) / 'events.sqlite3')
        self.assertEqual(self.store.settings(1)['channel_id'], 456)
        self.assertEqual(self.store.settings(1)['role_id'], 987)

    def test_ongoing_port_inspection_can_be_added_without_retroactive_reminders(self):
        now = int(datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc).timestamp())
        start = parse_utc('2026-11-06 00:00:00', now=now, future=False)
        end = parse_utc('2026-11-08 23:59:59', now=now, future=False)
        event_id = self.store.create(
            1, 2, 'Port Inspection', 'Port Inspection', 'Game event',
            start, 5, now=now, ends_at=end
        )
        event = self.store.event(event_id)
        self.assertEqual(event['starts_at'], start)
        self.assertEqual(event['ends_at'], end)
        self.assertEqual(len(self.store.upcoming(1, now=now)), 1)
        self.assertEqual(self.store.due(now=now), [])
        self.assertFalse(self.store.subscribe(event_id, 42, now=now))
        self.assertTrue(self.store.edit(event_id, 1, 'Port Inspection', 'Port Inspection',
                                        'Updated', start, end, now=now))

    def test_ended_event_is_rejected_while_ongoing_one_is_valid(self):
        now = int(datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc).timestamp())
        start = int(datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc).timestamp())
        with self.assertRaisesRegex(ValueError, 'already ended'):
            self.store.create(1, 2, 'Old event', 'Other', '', start, 5,
                              now=now, ends_at=now - 1)
        with self.assertRaisesRegex(ValueError, 'at least 5 minutes'):
            self.store.create(1, 2, 'Soon event', 'Other', '', now + 60, 5,
                              now=now, ends_at=now + 3600)
        self.assertIsNone(self.store.event(1))

    def test_second_precision_matches_in_game_calendar(self):
        start = parse_utc('2026-11-06 00:00:00', now=int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()))
        end = parse_utc('2026-11-08 23:59:59', now=0, future=False)
        self.assertEqual(datetime.fromtimestamp(start, timezone.utc).isoformat(), '2026-11-06T00:00:00+00:00')
        self.assertEqual(datetime.fromtimestamp(end, timezone.utc).isoformat(), '2026-11-08T23:59:59+00:00')
        self.assertEqual(end - start, 3 * 86400 - 1)

    def test_colon_separated_dates_and_minute_only_backwards_compatibility(self):
        self.assertEqual(
            parse_utc('2026:11:08 23:59:59', now=int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())),
            parse_utc('2026-11-08 23:59:59', now=int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()))
        )
        self.assertEqual(
            parse_utc('2026-11-08 19:00', now=int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())),
            parse_utc('2026-11-08 19:00:00', now=int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()))
        )
        with self.assertRaises(ValueError):
            parse_utc('2026-11-08 24:00:00', now=0)
        with self.assertRaises(ValueError):
            parse_utc('2026-11-08 19:00:99', now=0)

    def test_second_precision_persisted_as_distinct_event_endpoints(self):
        event_id = self.store.create(
            1, 2, 'Port Inspection', 'Port Inspection', 'Look for hidden rewards',
            self.now + 3600, 7, now=self.now, ends_at=self.now + 7201
        )
        row = self.store.event(event_id)
        self.assertEqual(row['ends_at'] - row['starts_at'], 3601)
        self.assertTrue(self.store.edit(
            event_id, 1, 'Port Inspection', 'Port Inspection', 'Updated',
            self.now + 6000, self.now + 8003, now=self.now
        ))
        self.assertEqual(self.store.event(event_id)['ends_at'], self.now + 8003)

    def test_parse_utc_uses_utc_and_rejects_outdated_or_invalid(self):
        valid = parse_utc('2027-01-15 18:00', now=1800000000)
        self.assertEqual(valid, 1800036000)
        with self.assertRaises(ValueError):
            parse_utc('not a date',now=1800000000)
        with self.assertRaises(ValueError):
            parse_utc('2020-01-01 18:00',now=1800000000)



class EventCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_register_as_server_group(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'DATABASE_PATH': str(Path(folder) / 'verify.json')}):
                bot = VerificationBot()
                bot.guild = discord.Object(id=1234)
                bot.events.register()
                commands = bot.tree.get_commands(guild=bot.guild)
                self.assertIn('event', [command.name for command in commands])
                group = next(command for command in commands if command.name == 'event')
                self.assertEqual(
                    sorted(command.name for command in group.commands),
                    ['calendar', 'cancel', 'create', 'edit', 'setup', 'upcoming', 'updates']
                )
                await bot.close()


class CalendarDiscordTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_calendar_embed_and_navigation(self):
        with tempfile.TemporaryDirectory() as folder:
            class Stub:
                guild = discord.Object(id=1)
            manager = EventCenter(Stub(), Path(folder) / 'calendar.sqlite3')
            now = int(datetime.now(timezone.utc).timestamp())
            manager.store.create(1, 123, 'Alliance Mobilization', 'Alliance', 'Prepare your members',
                                 now + 3600, 19, now, ends_at=now + 2 * 86400)
            embed, attachment = manager.calendar_payload(1)
            self.assertEqual(embed.title, 'SaS | Weekly Event Calendar')
            self.assertTrue(any(field.name == 'EVENT TIMES | SERVER CLOCK' for field in embed.fields))
            self.assertEqual(Image.open(attachment.fp).format, 'PNG')
            controls = CalendarNavigation(manager, 19)
            self.assertEqual([button.label for button in controls.children],
                             ['Previous Week', 'This Week', 'Next Week', 'Upcoming Events'])
            manager.close()


class SharedEventBoardTests(unittest.IsolatedAsyncioTestCase):
    async def test_board_is_published_once_then_edited(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 88
            channel.send = AsyncMock()
            channel.fetch_message = AsyncMock()
            message = MagicMock()
            message.id = 12345
            message.jump_url = 'https://discord.com/channels/1/88/12345'
            message.edit = AsyncMock(return_value=message)
            channel.send.return_value = message
            channel.fetch_message.return_value = message
            guild = MagicMock()
            guild.id = 1
            guild.get_channel.return_value = channel
            bot = MagicMock()
            bot.guild = guild
            with patch.dict(os.environ, {'EVENT_BOARD_CHANNEL_ID': '88'}):
                manager = EventCenter(bot, Path(folder) / 'events.sqlite3')
            now = int(datetime.now(timezone.utc).timestamp())
            manager.store.set_channel(1, 88, None)
            manager.store.create(1, 88, 'Port Inspection', 'Port Inspection', '',
                                 now - 3600, 7, now=now, ends_at=now + 7200)
            manager.store.create(1, 88, 'Work Log', 'Port Inspection', '',
                                 now - 3600, 7, now=now, ends_at=now + 86400)
            first = await manager.ensure_board(1, create=True)
            self.assertEqual(first.id, 12345)
            self.assertEqual(channel.send.await_count, 1)
            embed = channel.send.await_args.kwargs['embed']
            attachment = channel.send.await_args.kwargs['file']
            self.assertEqual(embed.title, 'SaS | Event Center')
            image = Image.open(attachment.fp)
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.width, 1000)
            self.assertGreater(image.height, 400)
            await manager.ensure_board(1, create=True)
            self.assertEqual(channel.send.await_count, 1)
            self.assertEqual(message.edit.await_count, 1)
            self.assertEqual(manager.store.settings(1)['calendar_message_id'], 12345)
            manager.close()

    async def test_existing_board_moves_to_requested_channel_and_removes_old_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            reminders = MagicMock(spec=discord.TextChannel)
            reminders.id = 88
            board_channel = MagicMock(spec=discord.TextChannel)
            board_channel.id = 1568424573836464208
            old_message = MagicMock()
            old_message.id = 1111
            old_message.author.id = 777
            old_message.delete = AsyncMock()
            reminders.fetch_message = AsyncMock(return_value=old_message)
            new_message = MagicMock()
            new_message.id = 2222
            new_message.jump_url = 'https://discord.com/channels/1/1568424573836464208/2222'
            new_message.author.id = 777
            new_message.edit = AsyncMock(return_value=new_message)
            board_channel.send = AsyncMock(return_value=new_message)
            board_channel.fetch_message = AsyncMock(return_value=new_message)
            guild = MagicMock()
            guild.id = 1
            guild.get_channel.side_effect = lambda channel_id: {88: reminders, 1568424573836464208: board_channel}.get(channel_id)
            bot = MagicMock()
            bot.user.id = 777
            bot.guild = guild
            manager = EventCenter(bot, Path(folder) / 'events.sqlite3')
            now = int(datetime.now(timezone.utc).timestamp())
            manager.store.set_channel(1, 88, None)
            manager.store.set_board(1, 1111, now=now)
            self.assertEqual(manager.store.settings(1)['calendar_message_channel_id'], 88)
            self.assertEqual([r['guild_id'] for r in manager.store.boards_to_refresh(now+60, manager.board_channel_id)], [1])
            record_id = manager.store.create(1, 88, 'Work Log', 'Port Inspection', '', now-60, 5, now=now, ends_at=now+6000)
            new = await manager.ensure_board(1, create=True)
            self.assertEqual(new.id, 2222)
            self.assertEqual(board_channel.send.await_count, 1)
            self.assertEqual(reminders.send.await_count, 0)
            self.assertEqual(old_message.delete.await_count, 1)
            self.assertEqual(manager.store.settings(1)['calendar_message_id'], 2222)
            self.assertEqual(manager.store.settings(1)['calendar_message_channel_id'], 1568424573836464208)
            await manager.ensure_board(1, create=True)
            self.assertEqual(board_channel.send.await_count, 1)
            self.assertEqual(new_message.edit.await_count, 1)
            manager.close()

    async def test_invalid_board_channel_does_not_post_into_reminder_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            reminders = MagicMock(spec=discord.TextChannel)
            reminders.id = 88
            reminders.send = AsyncMock()
            guild = MagicMock()
            guild.id = 1
            guild.get_channel.side_effect = lambda channel_id: reminders if channel_id == 88 else None
            bot = MagicMock()
            bot.guild = guild
            manager = EventCenter(bot, Path(folder) / 'events.sqlite3')
            manager.store.set_channel(1, 88, None)
            self.assertIsNone(await manager.ensure_board(1, create=True))
            reminders.send.assert_not_awaited()
            manager.close()

    async def test_existing_event_image_removed_and_footer_fixed(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 88
            message = MagicMock()
            message.id = 555
            message.edit = AsyncMock(return_value=message)
            channel.fetch_message = AsyncMock(return_value=message)
            guild = MagicMock()
            guild.id = 1
            guild.get_channel.return_value = channel
            bot = MagicMock()
            bot.guild = guild
            manager = EventCenter(bot, Path(folder) / 'events.sqlite3')
            now = int(datetime.now(timezone.utc).timestamp())
            event_id = manager.store.create(1, 88, 'Work Log', 'Port Inspection', '',
                                            now - 120, 7, now=now, ends_at=now + 43200)
            manager.store.set_message(event_id, message.id, 'https://example.com/old_event_banner.png')
            manager.store.conn.execute('UPDATE game_events SET display_phase=NULL WHERE id=?',(event_id,))
            record = manager.store.event(event_id)
            self.assertEqual([row['id'] for row in manager.store.cards_to_refresh(now)], [event_id])
            await manager.sync_event_card(record)
            opts = message.edit.await_args.kwargs
            self.assertEqual(opts['attachments'], [])
            self.assertIsNone(opts['view'])
            embed = opts['embed']
            self.assertEqual(next(field.value for field in embed.fields if field.name == 'STATUS'), 'In Progress')
            self.assertEqual(embed.footer.text, f'SaS Event Center | Event #{event_id} | In Progress')
            self.assertIsNone(embed.image.url)
            self.assertEqual(manager.store.event(event_id)['display_phase'], 'In Progress')
            self.assertIsNone(manager.store.event(event_id)['banner_url'])
            manager.close()


class EventOverviewImageTests(unittest.TestCase):
    def test_multiple_events_render_together_in_a_single_png(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        events = [
            {'title':'Port Inspection', 'category':'Port Inspection', 'phase':'In Progress',
             'starts_at':int((now-timedelta(hours=2)).timestamp()),
             'ends_at':int((now+timedelta(days=2)).timestamp())},
            {'title':'Work Log', 'category':'Port Inspection', 'phase':'In Progress',
             'starts_at':int((now-timedelta(hours=2)).timestamp()),
             'ends_at':int((now+timedelta(days=2)).timestamp())},
            {'title':'Alliance Mobilization', 'category':'Alliance', 'phase':'Upcoming',
             'starts_at':int((now+timedelta(days=1)).timestamp()),
             'ends_at':int((now+timedelta(days=3)).timestamp())}
        ]
        image = Image.open(event_board(events, now))
        self.assertEqual(image.format, 'PNG')
        self.assertEqual(image.width, 1000)
        self.assertGreater(image.height, 550)
        self.assertGreater(image.height, Image.open(event_board(events[:1], now)).height)


class EventCreationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_events_share_one_text_feed_and_one_graphic(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {
                'DATABASE_PATH': str(Path(folder) / 'verify.json'),
                'EVENT_DATABASE_PATH': str(Path(folder) / 'events.sqlite3'),
                'EVENT_BOARD_CHANNEL_ID': '1568424573836464208',
                'EVENT_TEXT_CHANNEL_ID': '1568431370747908291'
            }):
                bot = VerificationBot()
                guild = MagicMock()
                guild.id = 1
                channels = {}
                stored = {}
                sent = []
                for channel_id in (88, 1568424573836464208, 1568431370747908291):
                    channel = MagicMock(spec=discord.TextChannel)
                    channel.id = channel_id
                    channel.mention = f'<#{channel_id}>'
                    async def create_msg(cid=channel_id, **kwargs):
                        msg = MagicMock()
                        msg.id = 500 + len(sent)
                        msg.jump_url = f'https://discord.com/channels/1/{cid}/{msg.id}'
                        msg.author.id = 777
                        msg.edit = AsyncMock(return_value=msg)
                        msg.delete = AsyncMock()
                        stored[msg.id] = msg
                        sent.append((cid, kwargs, msg))
                        return msg
                    async def fetch_msg(message_id):
                        return stored[message_id]
                    channel.send = AsyncMock(side_effect=create_msg)
                    channel.fetch_message = AsyncMock(side_effect=fetch_msg)
                    channels[channel_id] = channel
                guild.get_channel.side_effect = channels.get
                bot.guild = guild
                bot.events.is_staff = lambda interaction: True
                bot.events.store.set_channel(1, 88, None)
                bot.events.register()
                group = bot.tree.get_commands(guild=guild)[0]
                create = group.get_command('create')
                interaction = MagicMock()
                interaction.guild_id = 1
                interaction.guild = guild
                interaction.user.id = 99
                interaction.response.defer = AsyncMock()
                interaction.response.send_message = AsyncMock()
                interaction.followup.send = AsyncMock()
                clock = datetime.now(timezone.utc)
                start = (clock + timedelta(hours=6)).strftime('%Y-%m-%d %H:%M:%S')
                end = (clock + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
                for title in ('Port Inspection', 'Oilfield Strike'):
                    await create.callback(interaction, title=title, start_utc=start,
                                          category='Alliance', notes='See in-game details', end_utc=end)
                self.assertEqual([cid for cid,_,_ in sent], [1568431370747908291, 1568424573836464208])
                self.assertEqual(channels[88].send.await_count, 0)
                self.assertEqual(channels[1568431370747908291].send.await_count, 1)
                self.assertEqual(channels[1568424573836464208].send.await_count, 1)
                text_message = sent[0][2]
                graph_message = sent[1][2]
                self.assertEqual(text_message.edit.await_count, 1)
                self.assertEqual(graph_message.edit.await_count, 1)
                update = text_message.edit.await_args.kwargs['embed']
                self.assertEqual(update.title, 'SaS | Event Updates')
                self.assertTrue(any('Port Inspection' in field.name for field in update.fields))
                self.assertTrue(any('Oilfield Strike' in field.name for field in update.fields))
                self.assertEqual(bot.events.store.settings(1)['text_message_id'], text_message.id)
                self.assertEqual(bot.events.store.settings(1)['text_message_channel_id'], 1568431370747908291)
                self.assertEqual(bot.events.store.board_event_count(1), 2)
                self.assertIsNone(group.get_command('remind'))
                self.assertIsNone(group.get_command('unremind'))
                await bot.close()

    async def test_shared_text_feed_migrates_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 1568431370747908291
            message = MagicMock()
            message.id = 739
            message.jump_url = 'https://discord.com/channels/1/1568431370747908291/739'
            message.edit = AsyncMock(return_value=message)
            channel.send = AsyncMock(return_value=message)
            channel.fetch_message = AsyncMock(return_value=message)
            guild = MagicMock()
            guild.id = 1
            guild.get_channel.side_effect = lambda cid: channel if cid == channel.id else None
            bot = MagicMock()
            bot.guild = guild
            manager = EventCenter(bot, Path(folder) / 'events.sqlite3')
            now = int(datetime.now(timezone.utc).timestamp())
            manager.store.set_channel(1, 88, None)
            manager.store.create(1, 88, 'Oilfield Strike', 'Alliance', 'Test', now+7200, 5, now, ends_at=now+10800)
            first = await manager.ensure_text_feed(1, create=True)
            self.assertEqual(first.id, 739)
            await manager.ensure_text_feed(1, create=True)
            self.assertEqual(channel.send.await_count, 1)
            self.assertEqual(message.edit.await_count, 1)
            manager.store.close()
            manager.store = EventStore(Path(folder) / 'events.sqlite3')
            self.assertEqual(manager.store.settings(1)['text_message_id'], 739)
            self.assertEqual(manager.store.settings(1)['text_message_channel_id'], channel.id)
            manager.close()

    async def test_one_hour_ping_targets_exact_member_role_in_updates_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 1568431370747908291
            feed = MagicMock()
            feed.id = 8744
            feed.edit = AsyncMock(return_value=feed)
            ping = MagicMock()
            ping.id = 9876
            channel.send = AsyncMock(side_effect=[feed, ping])
            channel.fetch_message = AsyncMock(return_value=feed)
            perms = channel.permissions_for.return_value
            perms.view_channel = True
            perms.send_messages = True
            role = MagicMock(spec=discord.Role)
            role.id = 1562535352290250842
            role.mention = '<@&1562535352290250842>'
            role.mentionable = True
            guild = MagicMock()
            guild.id = 1
            guild.get_channel.side_effect = lambda cid: channel if cid == channel.id else None
            guild.get_role.side_effect = lambda rid: role if rid == role.id else None
            guild.me = MagicMock()
            bot = MagicMock()
            bot.guild = guild
            bot.configured = True
            bot.fetch_user = AsyncMock()
            manager = EventCenter(bot, Path(folder) / 'events.sqlite3')
            manager.refresh_board_safe = AsyncMock()
            manager.retire_legacy_cards = AsyncMock()
            manager.store.set_channel(1, 88, None)
            now = int(datetime.now(timezone.utc).timestamp())
            event_id = manager.store.create(
                1, 88, 'Oilfield Strike', 'Alliance', '',
                now + 3599, 5, now=now-100, ends_at=now+8000
            )
            await manager.ensure_text_feed(1, create=True)
            await manager.scheduler.coro(manager)
            self.assertEqual(channel.send.await_count, 2)
            self.assertEqual(feed.edit.await_count, 0)
            request = channel.send.await_args.kwargs
            self.assertIn('<@&1562535352290250842>', request['content'])
            self.assertIn('Oilfield Strike', request['content'])
            self.assertIn('1-hour event reminder', request['content'])
            self.assertIn('UTC', request['content'])
            self.assertTrue(request['allowed_mentions'].roles)
            self.assertEqual(request['allowed_mentions'].roles[0].id, role.id)
            self.assertFalse(request['allowed_mentions'].everyone)
            record = manager.store.conn.execute(
                "SELECT state,message_id FROM event_reminders WHERE event_id=? AND stage='1h'",
                (event_id,)
            ).fetchone()
            self.assertEqual((record['state'],record['message_id']), ('sent',9876))
            await manager.scheduler.coro(manager)
            self.assertEqual(channel.send.await_count, 2)
            bot.fetch_user.assert_not_awaited()
            manager.close()

    async def test_missing_or_unmentionable_member_role_cannot_trigger_ping(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 1568431370747908291
            channel.send = AsyncMock()
            role = MagicMock(spec=discord.Role)
            role.id = 1562535352290250842
            role.mentionable = False
            guild = MagicMock()
            guild.id = 1
            guild.me = MagicMock()
            guild.get_channel.return_value = channel
            guild.get_role.return_value = role
            perms = channel.permissions_for.return_value
            perms.view_channel = True
            perms.send_messages = True
            perms.mention_everyone = False
            bot = MagicMock()
            bot.guild = guild
            manager = EventCenter(bot, Path(folder) / 'events.sqlite3')
            now = int(datetime.now(timezone.utc).timestamp())
            event_id = manager.store.create(1, 88, 'Work Log', 'Events', '',
                                            now+7200, 5, now=now, ends_at=now+8300)
            self.assertIsNone(await manager.send_member_ping(manager.store.event(event_id)))
            channel.send.assert_not_awaited()
            role.mentionable = True
            sent = MagicMock()
            sent.id = 22
            channel.send = AsyncMock(return_value=sent)
            self.assertEqual((await manager.send_member_ping(manager.store.event(event_id))).id, 22)
            manager.close()

    async def test_legacy_event_card_deleted_only_when_bot_owned(self):
        with tempfile.TemporaryDirectory() as folder:
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 88
            message = MagicMock()
            message.id = 199
            message.author.id = 777
            message.delete = AsyncMock()
            channel.fetch_message = AsyncMock(return_value=message)
            guild = MagicMock()
            guild.id = 1
            guild.get_channel.return_value = channel
            bot = MagicMock()
            bot.guild = guild
            bot.user.id = 777
            manager = EventCenter(bot, Path(folder) / 'events.sqlite3')
            now = int(datetime.now(timezone.utc).timestamp())
            record = manager.store.create(1, 88, 'Work Log', 'Alliance', '', now+7200, 5, now, ends_at=now+10000)
            manager.store.set_message(record, 199)
            await manager.retire_legacy_cards(1)
            message.delete.assert_awaited_once()
            self.assertIsNone(manager.store.event(record)['message_id'])
            manager.close()

class CalendarImageTests(unittest.TestCase):
    def test_illustrated_calendar_shows_week_and_event_ranges(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        first_day = now.date() - timedelta(days=2)
        record = {
            'title': 'Alliance Training',
            'category': 'Alliance',
            'starts_at': int((now + timedelta(hours=5)).timestamp()),
            'ends_at': int((now + timedelta(days=2)).timestamp())
        }
        image = Image.open(weekly_calendar([record], first_day, server_now=now, demo=True))
        self.assertEqual(image.format, 'PNG')
        self.assertEqual(image.width, 1120)
        self.assertGreater(image.height, 400)


if __name__ == '__main__':
    unittest.main()
