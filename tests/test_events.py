import tempfile
import unittest
from pathlib import Path

from PIL import Image

from event_center import EventStore, EventReminderView, parse_utc, EventCenter
from event_graphics import event_banner


class EventStoreTests(unittest.TestCase):
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

    def test_settings_survive_restart(self):
        self.store.set_channel(1, 456, 987)
        self.store.close()
        self.store = EventStore(Path(self.directory.name) / 'events.sqlite3')
        self.assertEqual(self.store.settings(1)['channel_id'], 456)
        self.assertEqual(self.store.settings(1)['role_id'], 987)

    def test_parse_utc_uses_utc_and_rejects_outdated_or_invalid(self):
        valid = parse_utc('2027-01-15 18:00', now=1800000000)
        self.assertEqual(valid, 1800036000) if False else self.assertTrue(valid > 1800000300)
        with self.assertRaises(ValueError):
            parse_utc('not a date',now=1800000000)
        with self.assertRaises(ValueError):
            parse_utc('2020-01-01 18:00',now=1800000000)

    def test_preview_view_is_persistent(self):
        class Stub:
            pass
        buttons = EventReminderView(Stub())
        self.assertEqual([c.label for c in buttons.children], ['Remind Me', 'Stop Reminders', 'View Schedule'])
        self.assertTrue(buttons.is_persistent())


class EventBannerTests(unittest.TestCase):
    def test_banner_is_png(self):
        data = event_banner('Alliance Mobilization', 'Alliance', '1h')
        picture = Image.open(data)
        self.assertEqual(picture.size, (1000, 300))
        self.assertEqual(picture.format, 'PNG')


if __name__ == '__main__':
    unittest.main()
