import ast
import io
import json
from pathlib import Path
import tempfile
import tokenize
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from database import Database
from views import ApplicationModal, ReviewView, VerifyView, valid_username
import test_bot


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/data.json'
        self.db = Database(self.path)
        self.row = self.db.create(1, 2, 'Player', 'de', {'patrol': 'Evenings', 'experience': 'New'}, 'Rules v1')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def act(self, action, actor=3, detail=''):
        row = self.db.get(self.row['id'])
        return self.db.transition(row['id'], actor, action, row['revision'], detail)

    def test_cancel_then_reapply(self):
        self.act('cancelled', 2)
        self.assertIsNotNone(self.db.create(1, 2, 'New name'))
        with self.assertRaises(ValueError):
            self.act('accepted')

    def test_other_user_cannot_cancel_or_answer(self):
        for action in ('cancelled', 'answer'):
            with self.assertRaises(ValueError):
                self.act(action, 8, 'Something')

    def test_claim_blocks_other_staff_and_can_release(self):
        self.act('claim')
        for action in ('claim', 'release', 'accepted', 'rejected', 'question'):
            with self.assertRaises(ValueError):
                self.act(action, 4, 'Text')
        self.act('release')
        self.act('claim', 4)
        self.assertEqual(self.db.get(1)['claimed_by'], 4)

    def test_stale_modal_cannot_overwrite_question_or_claim(self):
        self.act('claim')
        with self.assertRaises(ValueError):
            self.db.transition(1, 3, 'accepted', 0)

    def test_question_answer_persist_after_restart(self):
        self.act('question', detail='What is your timezone?')
        self.act('answer', 2, 'Europe/Zurich')
        self.db = Database(self.path)
        self.assertEqual(self.db.get(1)['answer'], 'Europe/Zurich')
        with self.assertRaises(ValueError):
            self.act('answer', 2, 'Different')

    def test_undo_acceptance_clears_verified_state(self):
        self.act('accepted', detail='Welcome aboard')
        self.assertTrue(self.db.member_state(1, 2)['verified'])
        self.act('undo', detail='Wrong request')
        self.assertFalse(self.db.member_state(1, 2)['verified'])
        self.assertEqual(self.db.get(1)['status'], 'pending')
        self.act('rejected', detail='Incorrect username')
        self.act('undo', detail='Correction')
        self.assertEqual(self.db.get(1)['status'], 'pending')

    def test_revoke_and_rejoin_do_not_restore_member(self):
        self.act('accepted')
        self.act('revoked', detail='Verification no longer valid')
        self.db.track_member(1, 2)
        self.assertFalse(self.db.member_state(1, 2)['verified'])
        with self.assertRaises(ValueError):
            self.act('undo', detail='Not a decision')

    def test_returning_verified_member_is_remembered(self):
        self.act('accepted')
        self.db.roles_applied(1, 2)
        self.db = Database(self.path)
        self.db.track_member(1, 2)
        self.assertTrue(self.db.member_state(1, 2)['verified'])
        self.assertTrue(self.db.member_state(1, 2)['dirty'])

    def test_newer_request_prevents_undo_of_old_rejection(self):
        self.act('rejected', detail='Wrong name')
        self.db.create(1, 2, 'Correct name')
        with self.assertRaises(ValueError):
            self.act('undo', detail='Try reopening')

    def test_queue_and_pause_apply_at_submission(self):
        self.db.configure(3, False, '', 1)
        with self.assertRaises(ValueError):
            self.db.create(1, 7, 'Other player')
        self.act('cancelled', 2)
        self.db.configure(3, True, 'Staff unavailable', 0)
        with self.assertRaises(ValueError):
            self.db.create(1, 7, 'Other player')
        self.db.configure(3, False, '', 0)
        self.assertIsNotNone(self.db.create(1, 7, 'Other player'))

    def test_duplicate_username_is_flag_not_rejection(self):
        other = self.db.create(1, 7, 'PLAYER')
        self.assertEqual(self.db.duplicates(other), [2])
        self.act('cancelled', 2)
        self.assertEqual(self.db.duplicates(other), [])

    def test_self_review_and_blank_reasons_are_rejected(self):
        with self.assertRaises(ValueError):
            self.act('accepted', 2)
        for action in ('rejected', 'question'):
            with self.assertRaises(ValueError):
                self.act(action, detail='  ')

    def test_migration_preserves_legacy_acceptance_and_panel(self):
        data = json.loads(Path(self.path).read_text())
        data.pop('events')
        data.pop('members')
        data['settings']['panel_message'] = 99
        row = data['applications']['1']
        row.update(status='accepted', reviewer_id=3, roles_done=1, submission_dm='sent', decision_dm='sent')
        for key in ('revision', 'language', 'answers', 'rules', 'claimed_by', 'question', 'answer', 'note'):
            row.pop(key)
        Path(self.path).write_text(json.dumps(data))
        restored = Database(self.path)
        self.assertEqual(restored.setting('panel_message'), 99)
        self.assertTrue(restored.member_state(1, 2)['verified'])
        self.assertEqual(restored.get(1)['language'], 'en')
        self.assertEqual(restored.events(), [])

    def test_username_validation_is_platform_neutral(self):
        for value in ('Officer Smith', 'Officer_42', "O'Brien", 'Spieler-1'):
            self.assertTrue(valid_username(value))
        for value in ('', '   ', 'x' * 101, 'test\nname', 'https://example.com', '<@123>', 'name\u200b'):
            self.assertFalse(valid_username(value))


class WorkflowTests(test_bot.BotFixture, unittest.IsolatedAsyncioTestCase):
    async def test_undo_after_partial_acceptance_restores_progress(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.transition(row['id'], 3, 'accepted', 0)
        self.member.remove_roles.side_effect = discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'Denied')
        await self.bot.process(row['id'])
        self.bot.db.transition(row['id'], 3, 'undo', 1, 'Mistake')
        self.member.remove_roles.side_effect = None
        await self.bot.process(row['id'])
        self.assertEqual(self.member.add_roles.call_args.args[0], self.bot.ids['PROGRESS_ROLE_ID'])
        self.assertEqual(self.member.remove_roles.call_args.args[0], self.bot.ids['MEMBER_ROLE_ID'])
        messages = [c.kwargs['embed'].description for c in self.user.send.call_args_list]
        self.assertFalse(any('now have the Member role' in text for text in messages))

    async def test_audit_failure_does_not_block_roles_or_dm(self):
        self.bot.log_channel.send.side_effect = discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'Denied')
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.transition(row['id'], 3, 'accepted', 0, 'Good application')
        await self.bot.process(row['id'])
        self.assertEqual(self.bot.db.get(1)['roles_done'], 1)
        self.assertEqual(self.user.send.await_count, 2)
        self.assertFalse(self.bot.db.events()[0]['audit_done'])

    async def test_deleted_review_is_recreated(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.update(row['id'], message_id=100)
        self.bot.review_channel.fetch_message.side_effect = discord.NotFound(MagicMock(status=404, reason='Not found'), 'Deleted')
        await self.bot.sync_review(row['id'])
        self.assertEqual(self.bot.db.get(1)['message_id'], 50)
        self.bot.review_channel.send.assert_awaited_once()

    async def test_german_notification_contains_note_and_welcome(self):
        row = self.bot.db.create(1, 2, 'Player', 'de')
        self.bot.db.transition(row['id'], 3, 'accepted', 0, 'Gute Bewerbung')
        await self.bot.process(row['id'])
        description = self.user.send.call_args.kwargs['embed'].description
        self.assertIn('angenommen', description)
        self.assertIn('Gute Bewerbung', description)
        self.assertIn('Willkommen', description)

    async def test_rejoin_uses_persisted_approval(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.transition(row['id'], 3, 'accepted', 0)
        self.bot.configured = True
        self.member.id = 2
        self.member.guild.id = 1
        self.member.bot = False
        await self.bot.on_member_join(self.member)
        self.assertEqual(self.member.add_roles.call_args.args[0], self.bot.ids['MEMBER_ROLE_ID'])

    async def test_rules_and_questions_fit_discord_forms(self):
        modal = ApplicationModal(self.bot, 'de', 'Rules')
        self.assertEqual(len(modal.children), 3)
        self.assertTrue(all(item.required for item in modal.children))
        self.assertTrue(all(len(item.label) <= 45 for item in modal.children))
        for view in (ReviewView(self.bot), VerifyView(self.bot)):
            self.assertTrue(view.is_persistent())
            self.assertTrue(all(item.emoji is None for item in view.children))

    def interaction(self, user_id=3):
        interaction = MagicMock(guild_id=1, channel_id=self.bot.ids['REVIEW_CHANNEL_ID'])
        interaction.user.id = user_id
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()
        self.bot.configured = True
        return interaction

    async def test_role_removed_after_modal_open_cannot_decide(self):
        row = self.bot.db.create(1, 2, 'Player')
        interaction = self.interaction()
        self.member.roles = []
        await self.bot.apply_action(interaction, row, 'accepted')
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'pending')

    async def test_staff_modal_rechecks_revision(self):
        row = self.bot.db.create(1, 2, 'Player')
        self.bot.db.transition(row['id'], 3, 'claim', 0)
        interaction = self.interaction()
        self.member.roles = [MagicMock(id=self.bot.ids['STAFF_ROLE_ID'])]
        await self.bot.apply_action(interaction, row, 'accepted')
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'pending')

    async def test_applicant_cannot_cancel_another_users_application(self):
        row = self.bot.db.create(1, 2, 'Player')
        interaction = self.interaction(user_id=8)
        interaction.channel_id = self.bot.ids['VERIFY_CHANNEL_ID']
        await self.bot.apply_action(interaction, row, 'cancelled', applicant=True)
        self.assertEqual(self.bot.db.get(row['id'])['status'], 'pending')

    async def test_review_embed_fits_discord_limits(self):
        row = self.bot.db.create(1, 2, 'P' * 100, answers={'patrol': 'A' * 300, 'experience': 'B' * 600}, rules='R' * 3500)
        self.bot.db.transition(row['id'], 3, 'question', 0, 'Q' * 800)
        self.bot.db.transition(row['id'], 2, 'answer', 1, 'A' * 800)
        self.bot.db.transition(row['id'], 3, 'rejected', 2, 'N' * 800)
        result = self.bot.review_embed(self.bot.db.get(row['id']))
        self.assertLessEqual(len(result), 6000)
        self.assertLessEqual(len(result.fields), 25)
        self.assertTrue(all(len(field.value) <= 1024 for field in result.fields))

    async def test_transient_dm_failure_is_retried(self):
        self.user.send.side_effect = discord.HTTPException(MagicMock(status=503, reason='Unavailable'), 'Retry')
        row = self.bot.db.create(1, 2, 'Player')
        await self.bot.process(row['id'])
        self.assertFalse(self.bot.db.events()[0]['dm_done'])
        self.user.send.side_effect = None
        await self.bot.process(row['id'])
        self.assertTrue(self.bot.db.events()[0]['dm_done'])


class SourceTests(unittest.TestCase):
    def test_no_python_comments_docstrings_or_emoji(self):
        paths = list(Path('.').glob('*.py')) + list(Path('tests').glob('*.py'))
        for path in paths:
            source = path.read_text()
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            self.assertFalse(any(t.type == tokenize.COMMENT for t in tokens), str(path))
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertIsNone(ast.get_docstring(node), str(path))
            self.assertFalse(any(0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF for c in source), str(path))
