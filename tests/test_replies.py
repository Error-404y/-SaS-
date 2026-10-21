import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.webhook.async_ import async_context

from views import SafeView, reply


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = MagicMock()
        self.adapter.execute_webhook = AsyncMock(return_value={})
        self.context = async_context.set(self.adapter)
        self.state = MagicMock()
        self.state.allowed_mentions = discord.AllowedMentions.none()
        self.webhook = discord.Webhook(
            {'id': '123', 'type': 3, 'token': 'test-token'},
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

    async def test_deferred_reply_without_view_uses_real_webhook_validation(self):
        await reply(self.interaction, 'Saved', 'Your application was withdrawn.')
        self.adapter.execute_webhook.assert_awaited_once()
        call = self.adapter.execute_webhook.call_args.kwargs
        self.assertFalse(call['with_components'])
        self.assertEqual(call['payload']['embeds'][0]['title'], 'Saved')
        self.assertEqual(call['payload']['flags'] & 64, 64)

    async def test_followup_with_view_preserves_components(self):
        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(label='Continue', custom_id='test:continue'))
        await reply(self.interaction, 'Next step', view=view)
        call = self.adapter.execute_webhook.call_args.kwargs
        self.assertTrue(call['with_components'])
        self.assertEqual(call['payload']['components'][0]['components'][0]['label'], 'Continue')

    async def test_error_handler_can_reply_after_defer(self):
        with self.assertLogs('verification', level='ERROR'):
            await SafeView().on_error(self.interaction, ValueError('Test failure'), None)
        self.adapter.execute_webhook.assert_awaited_once()
        payload = self.adapter.execute_webhook.call_args.kwargs['payload']
        self.assertEqual(payload['embeds'][0]['title'], 'Unable to complete action')

    async def test_initial_reply_omits_absent_view(self):
        self.interaction.response.is_done.return_value = False
        await reply(self.interaction, 'Saved')
        self.interaction.response.send_message.assert_awaited_once()
        self.assertNotIn('view', self.interaction.response.send_message.call_args.kwargs)
        self.adapter.execute_webhook.assert_not_awaited()
