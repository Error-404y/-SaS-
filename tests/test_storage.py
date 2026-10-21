import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import Database


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'verification.json'
        self.db = Database(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_pending_request_is_blocked(self):
        self.assertIsNotNone(self.db.create(1, 2, 'Player'))
        self.assertIsNone(self.db.create(1, 2, 'Player'))
        self.assertIsNotNone(self.db.create(1, 3, 'Other'))

    def test_only_first_decision_is_saved(self):
        row = self.db.create(1, 2, 'Player')
        self.assertTrue(self.db.decide(row['id'], 2, 'accepted'))
        self.assertFalse(self.db.decide(row['id'], 3, 'rejected', 'Too late'))
        self.assertEqual(self.db.get(row['id'])['reviewer_id'], 2)

    def test_rejection_requires_reason_and_allows_reapplication(self):
        row = self.db.create(1, 2, 'Player')
        with self.assertRaises(ValueError):
            self.db.decide(row['id'], 3, 'rejected', ' ')
        self.db.decide(row['id'], 3, 'rejected', 'Wrong username')
        self.assertIsNotNone(self.db.create(1, 2, 'Correct name'))

    def test_deleted_pending_request_can_be_resubmitted_after_restart(self):
        row = self.db.create(1, 2, 'Player')
        self.db.update(row['id'], message_id=50)
        self.db.delete_review(row['id'])
        restored = Database(self.path)
        self.assertTrue(restored.get(row['id'])['review_deleted'])
        self.assertEqual(restored.get(row['id'])['status'], 'cancelled')
        self.assertIsNotNone(restored.create(1, 2, 'Player'))
        self.assertFalse(restored.decide(row['id'], 3, 'accepted'))

    def test_deleted_accepted_request_remains_accepted(self):
        row = self.db.create(1, 2, 'Player')
        self.db.decide(row['id'], 3, 'accepted')
        self.db.delete_review(row['id'])
        self.assertEqual(self.db.get(row['id'])['status'], 'accepted')
        self.assertTrue(self.db.is_verified(1, 2))

    def test_completion_is_atomic_and_history_is_idempotent(self):
        row = self.db.create(1, 2, 'Player')
        self.db.decide(row['id'], 3, 'accepted')
        self.db.update(row['id'], transfer_started=True, transfer_eligible=True)
        self.db.finish_acceptance(row['id'])
        self.db.finish_acceptance(row['id'])
        restored = Database(self.path)
        self.assertTrue(restored.get(row['id'])['roles_done'])
        self.assertEqual(len(restored.histories()), 1)
        self.assertEqual(len(restored.histories()[0]['entries']), 1)

    def test_ineligible_completion_has_no_history(self):
        row = self.db.create(1, 2, 'Player')
        self.db.decide(row['id'], 3, 'accepted')
        self.db.finish_acceptance(row['id'])
        self.assertEqual(self.db.histories(), [])

    def test_failed_write_preserves_disk_and_memory(self):
        row = self.db.create(1, 2, 'Player')
        before = self.path.read_bytes()
        with patch('database.os.replace', side_effect=OSError('Disk error')):
            with self.assertRaises(OSError):
                self.db.decide(row['id'], 3, 'accepted')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.db.get(row['id'])['status'], 'pending')
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_bad_json_is_preserved(self):
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):
            Database(self.path)
        self.assertEqual(self.path.read_text(), '{broken')

    def test_legacy_migration_backups_and_does_not_replay_old_events(self):
        old = dict(version=1, next_id=4, settings={'panel_message': 123},
                   events=[{'action': 'roles'}], members={'1:3': {'verified': False}}, applications={})
        for identifier, status, message_id in ((1, 'accepted', 50), (2, 'pending', 51), (3, 'pending', None)):
            old['applications'][str(identifier)] = dict(id=identifier, guild_id=1, user_id=identifier + 1,
                username='Player', status=status, message_id=message_id, created_at='2026-10-21 19:48:28',
                decided_at=None, reviewer_id=None, reason='', roles_done=status == 'accepted',
                claimed_by=123, rules='Rules', language='de')
        original = json.dumps(old)
        self.path.write_text(original)
        restored = Database(self.path)
        self.assertEqual(restored.data['version'], 2)
        self.assertEqual(restored.panel(), 123)
        self.assertTrue(restored.is_verified(1, 2))
        self.assertFalse(restored.is_verified(1, 3))
        self.assertEqual(restored.histories(), [])
        self.assertEqual(restored.get(2)['status'], 'pending')
        self.assertEqual(restored.get(3)['status'], 'cancelled')
        self.assertTrue(restored.get(3)['review_deleted'])
        self.assertEqual(self.path.with_name(self.path.name + '.v1.bak').read_text(), original)
        self.assertNotIn('events', restored.data)
        self.assertNotIn('claimed_by', restored.get(1))
        self.assertEqual(Database(self.path).data, restored.data)

    def test_returned_rows_cannot_modify_saved_state(self):
        row = self.db.create(1, 2, 'Player')
        row['status'] = 'accepted'
        self.assertEqual(self.db.get(row['id'])['status'], 'pending')
