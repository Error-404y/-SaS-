import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path

from PIL import Image

from event_center import EventStore, EventReminderView, EventCenter, CalendarNavigation, parse_utc
from bot import VerificationBot
from unittest.mock import patch
import os
import discord
from event_graphics import event_banner, weekly_calendar


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

    def test_all_four_reminder_times(self):
        event_id = self.create()
        reminders = self.store.conn.execute(
            'SELECT stage,due_at FROM event_reminders WHERE event_id=? ORDER BY due_at',
            (event_id,)
        ).fetchall()
        self.assertEqual([row['stage'] for row in reminders], ['24h', '1h', '15m', 'start'])
        self.assertEqual([row['due_at'] for row in reminders],
                         [self.now + 3600, self.now + 86400, self.now + 89100, self.now + 90000])

    def test_late_creation_skips_missed_reminders(self):
        event_id = self.create(1800)
        stages = [row['stage'] for row in self.store.conn.execute(
            'SELECT stage FROM event_reminders WHERE event_id=?', (event_id,)
        )]
        self.assertEqual(stages, ['15m', 'start'])

    def test_subscriptions_and_dm_delivery_are_opt_in(self):
        event_id = self.create()
        self.assertTrue(self.store.subscribe(event_id, 42, self.now))
        self.store.reminder_state(event_id, '1h', 'sent', 123)
        jobs = self.store.conn.execute('SELECT * FROM event_dm_jobs WHERE event_id=?',(event_id,)).fetchall()
        self.assertEqual([(job['user_id'],job['stage']) for job in jobs], [(42,'1h')])
        self.store.unsubscribe(event_id, 42)
        self.assertEqual(self.store.subscriber_count(event_id), 0)
        self.assertEqual(self.store.conn.execute('SELECT state FROM event_dm_jobs').fetchone()['state'],'skipped')

    def test_one_reminder_can_only_be_marked_once(self):
        event_id = self.create()
        self.store.reminder_state(event_id,'24h','sent',100)
        self.store.reminder_state(event_id,'24h','sent',200)
        row = self.store.conn.execute('SELECT * FROM event_reminders WHERE event_id=? AND stage=?',(event_id,'24h')).fetchone()
        self.assertEqual(row['message_id'],100)

    def test_cancel_stops_all_pending(self):
        event_id = self.create()
        self.store.subscribe(event_id, 42, self.now)
        self.assertTrue(self.store.cancel(event_id, 1))
        self.assertFalse(self.store.subscribe(event_id, 42, self.now))
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

    def test_rescheduling_rebuilds_reminders_preserving_subscribers(self):
        event_id = self.create()
        self.store.subscribe(event_id, 19, self.now)
        self.store.reminder_state(event_id, '24h', 'sent', 884)
        new_start = self.now + 100000
        self.assertTrue(self.store.edit(event_id, 1, 'Alliance Training', 'Alliance', 'Ready',
                                        new_start, new_start + 7200, now=self.now))
        self.assertEqual(self.store.event(event_id)['title'], 'Alliance Training')
        self.assertEqual(self.store.subscriber_count(event_id), 1)
        scheduled = self.store.conn.execute(
            'SELECT stage,state,due_at FROM event_reminders WHERE event_id=?',
            (event_id,)
        ).fetchall()
        self.assertEqual(len(scheduled), 4)
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
        self.assertIsNone(self.store.settings(1)['calendar_message_id'])

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

    async def test_preview_view_is_persistent(self):
        class Stub:
            pass
        buttons = EventReminderView(Stub())
        self.assertEqual([c.label for c in buttons.children], ['Remind Me', 'Stop Reminders', 'View Schedule'])
        self.assertTrue(buttons.is_persistent())



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
                    ['calendar', 'cancel', 'create', 'edit', 'setup', 'upcoming']
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


class EventBannerTests(unittest.TestCase):
    def test_banner_is_png(self):
        data = event_banner('Alliance Mobilization', 'Alliance', '1h')
        picture = Image.open(data)
        self.assertEqual(picture.size, (1000, 300))
        self.assertEqual(picture.format, 'PNG')


if __name__ == '__main__':
    unittest.main()
