import ast
import io
from pathlib import Path
import tokenize
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.webhook.async_ import async_context

from time_utils import zurich_time
from views import SafeView, embed, reply, send_embed


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = MagicMock()
        self.adapter.execute_webhook = AsyncMock(return_value={})
        self.adapter.create_interaction_response = AsyncMock(return_value={})
        self.context = async_context.set(self.adapter)
        self.state = MagicMock()
        self.state.allowed_mentions = discord.AllowedMentions.none()
        self.webhook = discord.Webhook({'id': '123', 'type': 3, 'token': 'test-token'},
                                       session=MagicMock(), state=self.state)
        self.interaction = MagicMock()
        self.interaction.response.is_done.return_value = True
        self.interaction.response.send_message = AsyncMock()
        self.interaction.followup = self.webhook
        self.message_patch = patch.object(discord.Webhook, '_create_message', return_value=MagicMock(id=456))
        self.message_patch.start()

    async def asyncTearDown(self):
        self.message_patch.stop()
        async_context.reset(self.context)

    async def test_followup_omits_absent_view_with_real_library(self):
        await reply(self.interaction, 'Accepted')
        call = self.adapter.execute_webhook.call_args.kwargs
        self.assertFalse(call['with_components'])
        self.assertEqual(call['payload']['embeds'][0]['title'], 'Accepted')
        self.assertEqual(call['payload']['flags'] & 64, 64)

    async def test_initial_response_omits_absent_view_with_real_library(self):
        self.interaction._state = self.state
        self.interaction._session = MagicMock()
        self.interaction.type = discord.InteractionType.component
        self.interaction.response = discord.InteractionResponse(self.interaction)
        with patch('discord.interactions.InteractionCallbackResponse'):
            await reply(self.interaction, 'Already verified')
        self.assertTrue(self.interaction.response.is_done())
        self.adapter.create_interaction_response.assert_awaited_once()

    async def test_actual_view_is_serialized(self):
        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(label='Accept', custom_id='test:accept'))
        await send_embed(self.interaction, embed('Pending'), view)
        call = self.adapter.execute_webhook.call_args.kwargs
        self.assertTrue(call['with_components'])
        self.assertEqual(call['payload']['components'][0]['components'][0]['label'], 'Accept')

    async def test_acknowledged_response_falls_back_to_followup(self):
        self.interaction.response.is_done.return_value = False
        self.interaction.response.send_message.side_effect = discord.HTTPException(
            MagicMock(status=400, reason='Bad Request'), {'code': 40060, 'message': 'Already acknowledged'})
        await reply(self.interaction, 'Saved')
        self.adapter.execute_webhook.assert_awaited_once()

    async def test_error_handler_uses_safe_followup(self):
        with self.assertLogs('verification', level='ERROR'):
            await SafeView().on_error(self.interaction, ValueError('Test'), None)
        self.adapter.execute_webhook.assert_awaited_once()


class SourceTests(unittest.TestCase):
    def test_no_comments_docstrings_or_emoji_in_python(self):
        for path in list(Path('.').glob('*.py')) + list(Path('tests').glob('*.py')):
            source = path.read_text()
            self.assertFalse(any(t.type == tokenize.COMMENT for t in tokenize.generate_tokens(io.StringIO(source).readline)), str(path))
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertIsNone(ast.get_docstring(node), str(path))
            self.assertFalse(any(0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF for c in source), str(path))

    def test_zurich_time_supports_legacy_and_daylight_saving(self):
        self.assertEqual(zurich_time('2026-10-21 19:48:28'), '21.10.2026 21:48:28 CEST')
        self.assertEqual(zurich_time('2026-01-01T12:00:00+00:00'), '01.01.2026 13:00:00 CET')
