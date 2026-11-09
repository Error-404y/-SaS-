import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path

import discord
from discord.ext import tasks


log = logging.getLogger("incident.broadcast")
CONTROL_GUILD_ID = 1569493510715871232
CONTROL_CHANNEL_ID = 1569493511286300775
START_KEYWORD = "DataErrorRev.x"
FIX_KEYWORD = "FixDataError.x"
ERROR_TITLE = "SEV-2 | PERSISTENCE TIER DEGRADATION"
RESTORED_TITLE = "RESOLVED | PERSISTENCE TIER RECOVERY"
ERROR_DESCRIPTION = (
    "Controlled fault-injection scenario: database connection-pool saturation "
    "and elevated transaction commit latency at the persistence boundary. "
    "Dependent write paths are modeled as degraded while retry and circuit-breaker "
    "handling is evaluated."
)
RESTORED_DESCRIPTION = (
    "Fault-injection sequence terminated. Connection acquisition and transaction "
    "write-path availability have returned to nominal state within the exercise scope."
)
PREVIOUS_ERROR_DESCRIPTION = "Simulated database connectivity incident. Some automated features may be temporarily unavailable.\n\nNo action is required from members."
PREVIOUS_RESTORED_DESCRIPTION = "Simulation complete. Database service status has been restored."
LEGACY_ERROR_TITLE = "SYSTEM ALERT | DATABASE CONNECTIVITY"
LEGACY_RESTORED_TITLE = "SYSTEM STATUS | SERVICE RESTORED"
LEGACY_ERROR_DESCRIPTION = "A database connectivity interruption has been reported. Some automated features may be temporarily unavailable.\n\nNo action is required from members."
LEGACY_RESTORED_DESCRIPTION = "Database connectivity restored. All affected bot services are operating normally."
RECOVERY_DISPLAY_SECONDS = 60


def incident_embed(recovered=False):
    if recovered:
        result = discord.Embed(
            title=RESTORED_TITLE,
            description=RESTORED_DESCRIPTION,
            color=0x278567,
        )
        result.add_field(name="STATE", value="RECOVERED", inline=True)
        result.add_field(name="MODE", value="CONTROLLED EXERCISE", inline=True)
    else:
        result = discord.Embed(
            title=ERROR_TITLE,
            description=ERROR_DESCRIPTION,
            color=0xC54C4C,
        )
        result.add_field(name="STATE", value="DEGRADED / TRIAGE", inline=True)
        result.add_field(name="MODE", value="CONTROLLED EXERCISE", inline=True)
    return result


class IncidentBroadcast:
    def __init__(self, bot, path=None):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.control_guild_id = int(os.getenv("INCIDENT_CONTROL_GUILD_ID", CONTROL_GUILD_ID))
        self.control_channel_id = int(os.getenv("INCIDENT_CONTROL_CHANNEL_ID", CONTROL_CHANNEL_ID))
        self.allowed_operators = {
            int(item.strip())
            for item in os.getenv("INCIDENT_OPERATOR_USER_IDS", "").split(",")
            if item.strip().isdigit()
        }
        database_path = Path(path or os.getenv("INCIDENT_DATABASE_PATH", "data/incident_broadcast.sqlite3"))
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(database_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS incident_session (
                id INTEGER PRIMARY KEY CHECK (id=1),
                status TEXT NOT NULL DEFAULT 'idle',
                cleanup_due INTEGER
            );
            CREATE TABLE IF NOT EXISTS incident_messages (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('alert','restored')),
                status TEXT NOT NULL DEFAULT 'pending',
                retry_at INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO incident_session(id,status) VALUES(1,'idle');
            CREATE INDEX IF NOT EXISTS incident_cleanup_index
                ON incident_messages(status,retry_at);
        """)

    def state(self):
        return self.conn.execute("SELECT * FROM incident_session WHERE id=1").fetchone()

    def tracked_messages(self):
        return self.conn.execute(
            "SELECT * FROM incident_messages ORDER BY guild_id,channel_id,kind"
        ).fetchall()

    def _targets(self):
        targets = []
        bot_id = getattr(self.bot.user, "id", None)
        for guild in self.bot.guilds:
            me = guild.me or (guild.get_member(bot_id) if bot_id else None)
            if me is None:
                continue
            for channel in guild.text_channels:
                permissions = channel.permissions_for(me)
                if (permissions.view_channel and permissions.send_messages and permissions.embed_links
                        and permissions.read_message_history):
                    targets.append((guild.id, channel))
        return targets

    async def _send(self, guild_id, channel, kind):
        notice = incident_embed(recovered=(kind == "restored"))
        try:
            sent = await channel.send(
                embed=notice, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            log.exception("Unable to publish %s notice in guild %s channel %s", kind, guild_id, channel.id)
            return False
        self.conn.execute(
            """INSERT OR IGNORE INTO incident_messages(message_id,guild_id,channel_id,kind)
               VALUES(?,?,?,?)""",
            (sent.id, guild_id, channel.id, kind),
        )
        return True

    async def _publish_all(self, targets, kind):
        semaphore = asyncio.Semaphore(4)

        async def publish(target):
            guild_id, channel = target
            async with semaphore:
                return await self._send(guild_id, channel, kind)

        result = await asyncio.gather(*(publish(target) for target in targets))
        return sum(result)

    async def activate(self):
        async with self.lock:
            if self.state()["status"] != "idle":
                return None
            targets = self._targets()
            if not targets:
                return 0
            self.conn.execute(
                "UPDATE incident_session SET status='active',cleanup_due=NULL WHERE id=1"
            )
            count = await self._publish_all(targets, "alert")
            if count == 0:
                self.conn.execute(
                    "UPDATE incident_session SET status='idle' WHERE id=1"
                )
            return count

    async def resolve(self):
        async with self.lock:
            if self.state()["status"] != "active":
                return None
            records = self.conn.execute(
                """SELECT DISTINCT guild_id,channel_id FROM incident_messages
                   WHERE kind='alert' AND status='pending'"""
            ).fetchall()
            targets = []
            for record in records:
                channel = self.bot.get_channel(record["channel_id"])
                if isinstance(channel, discord.TextChannel):
                    targets.append((record["guild_id"], channel))
            self.conn.execute(
                "UPDATE incident_session SET status='cleanup',cleanup_due=? WHERE id=1",
                (int(time.time()) + RECOVERY_DISPLAY_SECONDS,),
            )
            count = await self._publish_all(targets, "restored")
            self.conn.execute(
                "UPDATE incident_session SET cleanup_due=? WHERE id=1",
                (int(time.time()) + RECOVERY_DISPLAY_SECONDS,),
            )
            return count

    async def handle_message(self, message):
        if message.author.bot or not message.guild:
            return False
        if message.guild.id != self.control_guild_id or message.channel.id != self.control_channel_id:
            return False
        command = message.content.strip()
        if command not in (START_KEYWORD, FIX_KEYWORD):
            return False
        if (
            not getattr(message.author.guild_permissions, "administrator", False)
            or self.allowed_operators and message.author.id not in self.allowed_operators
        ):
            await message.reply(
                "Administrator authorization is required for incident control.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
                delete_after=20,
            )
            return True
        if command == START_KEYWORD:
            count = await self.activate()
            detail = (
                "An incident broadcast is already active or is awaiting cleanup."
                if count is None
                else f"Simulated database incident published in {count} accessible channel(s)."
            )
        else:
            count = await self.resolve()
            detail = (
                "No active incident is available for recovery."
                if count is None
                else f"Recovery published in {count} channel(s). Both notices will be removed after 60 seconds."
            )
        await message.reply(
            detail,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            delete_after=20,
        )
        return True

    async def cleanup_once(self, now=None):
        now = int(time.time()) if now is None else now
        async with self.lock:
            state = self.state()
            if state["status"] != "cleanup" or (state["cleanup_due"] or 0) > now:
                return 0
            records = self.conn.execute(
                """SELECT * FROM incident_messages
                   WHERE status='pending' AND retry_at<=?
                   ORDER BY kind,channel_id LIMIT 35""",
                (now,),
            ).fetchall()
            removed = 0
            for record in records:
                message_id = record["message_id"]
                channel = self.bot.get_channel(record["channel_id"])
                if not isinstance(channel, discord.TextChannel):
                    self.conn.execute(
                        "UPDATE incident_messages SET retry_at=? WHERE message_id=?",
                        (now + 120, message_id),
                    )
                    continue
                try:
                    posted = await channel.fetch_message(message_id)
                except discord.NotFound:
                    self.conn.execute(
                        "UPDATE incident_messages SET status='missing' WHERE message_id=?",
                        (message_id,),
                    )
                    continue
                except discord.HTTPException:
                    log.exception("Unable to fetch incident message %s", message_id)
                    self.conn.execute(
                        "UPDATE incident_messages SET retry_at=? WHERE message_id=?",
                        (now + 120, message_id),
                    )
                    continue
                accepted = (
                    (
                        (ERROR_TITLE, ERROR_DESCRIPTION),
                        (LEGACY_ERROR_TITLE, PREVIOUS_ERROR_DESCRIPTION),
                        (LEGACY_ERROR_TITLE, LEGACY_ERROR_DESCRIPTION),
                    )
                    if record["kind"] == "alert"
                    else (
                        (RESTORED_TITLE, RESTORED_DESCRIPTION),
                        (LEGACY_RESTORED_TITLE, PREVIOUS_RESTORED_DESCRIPTION),
                        (LEGACY_RESTORED_TITLE, LEGACY_RESTORED_DESCRIPTION),
                    )
                )
                if (
                    self.bot.user is None
                    or posted.author.id != self.bot.user.id
                    or not posted.embeds
                    or (posted.embeds[0].title, posted.embeds[0].description) not in accepted
                ):
                    self.conn.execute(
                        "UPDATE incident_messages SET status='ignored' WHERE message_id=?",
                        (message_id,),
                    )
                    continue
                try:
                    await posted.delete()
                except discord.NotFound:
                    self.conn.execute(
                        "UPDATE incident_messages SET status='missing' WHERE message_id=?",
                        (message_id,),
                    )
                except discord.HTTPException:
                    log.exception("Unable to delete incident message %s", message_id)
                    self.conn.execute(
                        "UPDATE incident_messages SET retry_at=? WHERE message_id=?",
                        (now + 120, message_id),
                    )
                else:
                    removed += 1
                    self.conn.execute(
                        "UPDATE incident_messages SET status='deleted' WHERE message_id=?",
                        (message_id,),
                    )
            still_pending = self.conn.execute(
                "SELECT COUNT(*) FROM incident_messages WHERE status='pending'"
            ).fetchone()[0]
            if still_pending == 0:
                self.conn.execute("DELETE FROM incident_messages")
                self.conn.execute(
                    "UPDATE incident_session SET status='idle',cleanup_due=NULL WHERE id=1"
                )
            return removed

    @tasks.loop(seconds=15)
    async def cleanup_loop(self):
        await self.cleanup_once()

    @cleanup_loop.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.cleanup_loop.is_running():
            self.cleanup_loop.start()

    def close(self):
        if self.cleanup_loop.is_running():
            self.cleanup_loop.cancel()
        self.conn.close()
