import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from incident_broadcast import (
    CONTROL_CHANNEL_ID,
    CONTROL_GUILD_ID,
    ERROR_TITLE,
    LEGACY_ERROR_TITLE,
    LEGACY_RESTORED_TITLE,
    LEGACY_ERROR_DESCRIPTION,
    PREVIOUS_RESTORED_DESCRIPTION,
    FIX_KEYWORD,
    RESTORED_TITLE,
    START_KEYWORD,
    IncidentBroadcast,
    incident_embed,
)


class IncidentBroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "incident.sqlite3"
        self.sent = []
        self.messages = {}
        self.channels = {}
        self.bot = MagicMock()
        self.bot.user.id = 8080
        self.bot.wait_until_ready = AsyncMock()
        self.bot.get_channel.side_effect = lambda channel_id: self.channels.get(channel_id)
        self.bot.guilds = []
        for guild_id, channel_id in (
            (CONTROL_GUILD_ID, CONTROL_CHANNEL_ID),
            (110, 120),
            (110, 121),
            (210, 220),
        ):
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = channel_id
            channel.guild.id = guild_id
            permissions = channel.permissions_for.return_value
            permissions.view_channel = True
            permissions.send_messages = True
            permissions.embed_links = True
            permissions.read_message_history = True

            async def send_message(cid=channel_id, **kwargs):
                message = MagicMock()
                message.id = 5000 + len(self.sent)
                message.author.id = self.bot.user.id
                message.embeds = [kwargs["embed"]]
                message.delete = AsyncMock()
                self.sent.append((cid, kwargs))
                self.messages[message.id] = message
                return message

            async def fetch_message(message_id):
                return self.messages[message_id]

            channel.send = AsyncMock(side_effect=send_message)
            channel.fetch_message = AsyncMock(side_effect=fetch_message)
            self.channels[channel_id] = channel
            guild = next((g for g in self.bot.guilds if g.id == guild_id), None)
            if guild is None:
                guild = MagicMock()
                guild.id = guild_id
                guild.me = MagicMock()
                guild.text_channels = []
                self.bot.guilds.append(guild)
            guild.text_channels.append(channel)
        self.incident = IncidentBroadcast(self.bot, self.path)

    def tearDown(self):
        self.incident.close()
        self.folder.cleanup()

    def control_message(self, keyword, *, guild_id=CONTROL_GUILD_ID, channel_id=CONTROL_CHANNEL_ID, admin=True):
        msg = MagicMock()
        msg.author.id = 123
        msg.author.bot = False
        msg.author.guild_permissions.administrator = admin
        msg.guild.id = guild_id
        msg.channel.id = channel_id
        msg.content = keyword
        msg.reply = AsyncMock()
        return msg

    async def test_control_keywords_are_restricted_to_exact_server_and_channel(self):
        wrong_server = self.control_message(START_KEYWORD, guild_id=110)
        wrong_channel = self.control_message(START_KEYWORD, channel_id=120)
        normal = self.control_message("DataErrorRev.x extra")
        for candidate in (wrong_server, wrong_channel, normal):
            self.assertFalse(await self.incident.handle_message(candidate))
            candidate.reply.assert_not_awaited()
        self.assertEqual(self.incident.state()["status"], "idle")
        self.assertEqual(self.sent, [])

    async def test_admin_is_required_for_all_server_broadcast(self):
        unauthorized = self.control_message(START_KEYWORD, admin=False)
        self.assertTrue(await self.incident.handle_message(unauthorized))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.incident.state()["status"], "idle")
        unauthorized.reply.assert_awaited_once()

    async def test_optional_operator_whitelist(self):
        self.incident.close()
        with patch.dict(os.environ, {"INCIDENT_OPERATOR_USER_IDS": "999"}):
            self.incident = IncidentBroadcast(self.bot, self.path)
        user = self.control_message(START_KEYWORD)
        self.assertTrue(await self.incident.handle_message(user))
        self.assertEqual(self.sent, [])

    async def test_error_posts_in_every_accessible_channel_across_guilds_once(self):
        self.channels[121].permissions_for.return_value.send_messages = False
        message = self.control_message(START_KEYWORD)
        self.assertTrue(await self.incident.handle_message(message))
        self.assertEqual(set(cid for cid, _ in self.sent), {CONTROL_CHANNEL_ID, 120, 220})
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(all(kwargs["embed"].title == ERROR_TITLE for _, kwargs in self.sent))
        self.assertTrue(all(kwargs["allowed_mentions"].everyone is False for _, kwargs in self.sent))
        self.assertEqual(self.incident.state()["status"], "active")
        self.assertTrue(await self.incident.handle_message(message))
        self.assertEqual(len(self.sent), 3)

    async def test_recovery_and_both_notice_types_are_removed_after_one_minute(self):
        self.assertEqual(await self.incident.activate(), 4)
        fix = self.control_message(FIX_KEYWORD)
        self.assertTrue(await self.incident.handle_message(fix))
        self.assertEqual(len(self.sent), 8)
        titles = [kwargs["embed"].title for _, kwargs in self.sent]
        self.assertEqual(titles.count(ERROR_TITLE), 4)
        self.assertEqual(titles.count(RESTORED_TITLE), 4)
        due = self.incident.state()["cleanup_due"]
        self.assertEqual(await self.incident.cleanup_once(due - 1), 0)
        self.assertEqual(await self.incident.cleanup_once(due), 8)
        self.assertEqual(self.incident.state()["status"], "idle")
        self.assertEqual(len(self.incident.tracked_messages()), 0)
        self.assertTrue(all(message.delete.await_count == 1 for message in self.messages.values()))

    async def test_cleanup_never_deletes_other_bot_messages(self):
        self.assertEqual(await self.incident.activate(), 4)
        self.assertEqual(await self.incident.resolve(), 4)
        targeted_id = next(iter(self.messages))
        self.messages[targeted_id].author.id = 555
        due = self.incident.state()["cleanup_due"]
        removed = await self.incident.cleanup_once(due)
        self.assertEqual(removed, 7)
        self.messages[targeted_id].delete.assert_not_awaited()
        self.assertEqual(self.incident.state()["status"], "idle")

    async def test_recovery_cleanup_survives_restart(self):
        self.assertEqual(await self.incident.activate(), 4)
        self.assertEqual(await self.incident.resolve(), 4)
        due = self.incident.state()["cleanup_due"]
        self.incident.close()
        self.incident = IncidentBroadcast(self.bot, self.path)
        self.assertEqual(self.incident.state()["status"], "cleanup")
        self.assertEqual(await self.incident.cleanup_once(due), 8)
        self.assertEqual(self.incident.state()["status"], "idle")

    async def test_cleanup_accepts_older_incident_templates_after_upgrade(self):
        self.assertEqual(await self.incident.activate(), 4)
        self.assertEqual(await self.incident.resolve(), 4)
        alerts = [msg for msg in self.messages.values() if msg.embeds[0].title == ERROR_TITLE]
        recovery = [msg for msg in self.messages.values() if msg.embeds[0].title == RESTORED_TITLE]
        alerts[0].embeds[0].title = LEGACY_ERROR_TITLE
        alerts[0].embeds[0].description = LEGACY_ERROR_DESCRIPTION
        alerts[0].embeds[0].set_footer(text="System Communications | Simulated Incident")
        recovery[0].embeds[0].title = LEGACY_RESTORED_TITLE
        recovery[0].embeds[0].description = PREVIOUS_RESTORED_DESCRIPTION
        recovery[0].embeds[0].set_footer(text="System Communications | Simulated Incident")
        due = self.incident.state()["cleanup_due"]
        self.assertEqual(await self.incident.cleanup_once(due), 8)
        self.assertEqual(self.incident.state()["status"], "idle")

    def test_message_copy_is_technical_and_has_no_footer_or_embed_timestamp(self):
        alert = incident_embed()
        restored = incident_embed(recovered=True)
        self.assertIn("PERSISTENCE", alert.title)
        self.assertIn("connection-pool", alert.description)
        self.assertIn("transaction", restored.description)
        self.assertLess(len(alert.description), 310)
        self.assertLess(len(restored.description), 160)
        self.assertEqual(alert.fields[1].value, "CONTROLLED EXERCISE")
        self.assertEqual(restored.fields[1].value, "CONTROLLED EXERCISE")
        self.assertIsNone(alert.footer.text)
        self.assertIsNone(restored.footer.text)
        self.assertIsNone(alert.timestamp)
        self.assertIsNone(restored.timestamp)


if __name__ == "__main__":
    unittest.main()
