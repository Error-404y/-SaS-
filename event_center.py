import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from discord.ext import tasks

from event_graphics import event_banner


log = logging.getLogger('sas.events')
STAGES = (('24h', 86400), ('1h', 3600), ('15m', 900), ('start', 0))
CATEGORIES = ('Alliance', 'Events', 'Deals', 'Other')


class EventStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=5000')
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS event_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                role_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS game_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                notes TEXT NOT NULL,
                starts_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                message_id INTEGER,
                banner_url TEXT,
                state TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS event_subscribers (
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at INTEGER NOT NULL,
                PRIMARY KEY(event_id,user_id),
                FOREIGN KEY(event_id) REFERENCES game_events(id)
            );
            CREATE TABLE IF NOT EXISTS event_reminders (
                event_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                due_at INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                message_id INTEGER,
                PRIMARY KEY(event_id,stage),
                FOREIGN KEY(event_id) REFERENCES game_events(id)
            );
            CREATE TABLE IF NOT EXISTS event_dm_jobs (
                event_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(event_id,stage,user_id),
                FOREIGN KEY(event_id) REFERENCES game_events(id)
            );
        """)

    def set_channel(self, guild_id, channel_id, role_id):
        self.conn.execute(
            'INSERT INTO event_settings(guild_id,channel_id,role_id) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,role_id=excluded.role_id',
            (guild_id, channel_id, role_id)
        )

    def settings(self, guild_id):
        return self.conn.execute('SELECT * FROM event_settings WHERE guild_id=?', (guild_id,)).fetchone()

    def create(self, guild_id, channel_id, title, category, notes, starts_at, author_id, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            cursor = self.conn.execute(
                'INSERT INTO game_events(guild_id,channel_id,title,category,notes,starts_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)',
                (guild_id, channel_id, title, category, notes, starts_at, now, author_id)
            )
            event_id = cursor.lastrowid
            for stage, seconds in STAGES:
                due_at = starts_at - seconds
                if due_at >= now - 60:
                    self.conn.execute(
                        'INSERT INTO event_reminders(event_id,stage,due_at) VALUES(?,?,?)',
                        (event_id, stage, due_at)
                    )
            self.conn.execute('COMMIT')
            return event_id
        except Exception:
            self.conn.execute('ROLLBACK')
            raise

    def event(self, event_id):
        return self.conn.execute('SELECT * FROM game_events WHERE id=?', (event_id,)).fetchone()

    def by_message(self, message_id):
        return self.conn.execute('SELECT * FROM game_events WHERE message_id=?', (message_id,)).fetchone()

    def set_message(self, event_id, message_id, banner_url=None):
        self.conn.execute(
            'UPDATE game_events SET message_id=?,banner_url=? WHERE id=?',
            (message_id, banner_url, event_id)
        )

    def cancel(self, event_id, guild_id):
        cursor = self.conn.execute(
            "UPDATE game_events SET state='cancelled' WHERE id=? AND guild_id=? AND state='active'",
            (event_id, guild_id)
        )
        if cursor.rowcount:
            self.conn.execute("UPDATE event_reminders SET state='skipped' WHERE event_id=? AND state='pending'", (event_id,))
            self.conn.execute("UPDATE event_dm_jobs SET state='skipped' WHERE event_id=? AND state='pending'", (event_id,))
        return cursor.rowcount == 1

    def upcoming(self, guild_id, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        return self.conn.execute(
            "SELECT * FROM game_events WHERE guild_id=? AND state='active' AND starts_at>? ORDER BY starts_at LIMIT 15",
            (guild_id, now)
        ).fetchall()

    def subscribe(self, event_id, user_id, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        row = self.event(event_id)
        if row is None or row['state'] != 'active' or row['starts_at'] <= now:
            return False
        self.conn.execute(
            'INSERT OR IGNORE INTO event_subscribers(event_id,user_id,joined_at) VALUES(?,?,?)',
            (event_id, user_id, now)
        )
        return True

    def unsubscribe(self, event_id, user_id):
        self.conn.execute('DELETE FROM event_subscribers WHERE event_id=? AND user_id=?', (event_id, user_id))
        self.conn.execute("UPDATE event_dm_jobs SET state='skipped' WHERE event_id=? AND user_id=? AND state='pending'", (event_id, user_id))

    def subscriber_count(self, event_id):
        return self.conn.execute('SELECT COUNT(*) FROM event_subscribers WHERE event_id=?', (event_id,)).fetchone()[0]

    def due(self, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        return self.conn.execute(
            "SELECT r.*,e.guild_id,e.channel_id,e.starts_at,e.state AS event_state FROM event_reminders r JOIN game_events e ON e.id=r.event_id WHERE r.state='pending' AND r.due_at<=? ORDER BY r.due_at LIMIT 12",
            (now,)
        ).fetchall()

    def reminder_state(self, event_id, stage, state, message_id=None):
        self.conn.execute(
            'UPDATE event_reminders SET state=?,message_id=? WHERE event_id=? AND stage=? AND state=?',
            (state, message_id, event_id, stage, 'pending')
        )
        if state == 'sent' and stage in ('1h', '15m'):
            self.conn.execute(
                "INSERT OR IGNORE INTO event_dm_jobs(event_id,stage,user_id) SELECT s.event_id,?,s.user_id FROM event_subscribers s JOIN game_events e ON e.id=s.event_id WHERE s.event_id=? AND s.joined_at<=? AND e.state='active'",
                (stage, event_id, self.conn.execute('SELECT due_at FROM event_reminders WHERE event_id=? AND stage=?',(event_id,stage)).fetchone()[0])
            )

    def due_dm(self):
        return self.conn.execute(
            "SELECT d.*,e.guild_id,e.title,e.category,e.starts_at,e.channel_id,e.state AS event_state FROM event_dm_jobs d JOIN game_events e ON e.id=d.event_id WHERE d.state='pending' ORDER BY e.starts_at LIMIT 12"
        ).fetchall()

    def dm_state(self, event_id, stage, user_id, state):
        self.conn.execute(
            "UPDATE event_dm_jobs SET state=? WHERE event_id=? AND stage=? AND user_id=? AND state='pending'",
            (state,event_id,stage,user_id)
        )

    def close(self):
        self.conn.close()


def parse_utc(value, now=None):
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    try:
        start = datetime.strptime(value.strip(), '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError('Use YYYY-MM-DD HH:MM in game/server time (UTC), for example 2026-11-08 19:00.') from error
    timestamp = int(start.timestamp())
    if timestamp < now + 300 or timestamp > now + 90 * 86400:
        raise ValueError('Events must start between 5 minutes and 90 days from now.')
    return timestamp


def clean(value, limit):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))[:limit]


class EventReminderView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label='Remind Me', style=discord.ButtonStyle.primary, custom_id='sas:event:subscribe:v1')
    async def subscribe(self, interaction, button):
        event = self.manager.store.by_message(interaction.message.id)
        if not event or interaction.guild_id != event['guild_id']:
            return await interaction.response.send_message('Event not found.', ephemeral=True)
        if self.manager.store.subscribe(event['id'], interaction.user.id):
            await interaction.response.send_message('You are subscribed. The bot will DM you 1 hour and 15 minutes before the event, if you subscribe before those reminders are due.', ephemeral=True)
        else:
            await interaction.response.send_message('This event is no longer accepting subscriptions.', ephemeral=True)

    @discord.ui.button(label='Stop Reminders', style=discord.ButtonStyle.secondary, custom_id='sas:event:unsubscribe:v1')
    async def unsubscribe(self, interaction, button):
        event = self.manager.store.by_message(interaction.message.id)
        if not event or interaction.guild_id != event['guild_id']:
            return await interaction.response.send_message('Event not found.', ephemeral=True)
        self.manager.store.unsubscribe(event['id'], interaction.user.id)
        await interaction.response.send_message('Your reminders for this event are disabled.', ephemeral=True)

    @discord.ui.button(label='View Schedule', style=discord.ButtonStyle.secondary, custom_id='sas:event:list:v1')
    async def schedule(self, interaction, button):
        await self.manager.upcoming_message(interaction)


class EventCenter:
    def __init__(self, bot, path):
        self.bot = bot
        self.store = EventStore(path)
        self.group_registered = False

    def is_staff(self, interaction):
        return bool(
            self.bot.configured and self.bot.guild and interaction.guild_id == self.bot.guild.id
            and isinstance(interaction.user, discord.Member) and self.bot.is_reviewer(interaction.user)
        )

    def build_embed(self, event, stage='upcoming'):
        name = clean(event['title'], 200)
        status = 'Cancelled' if event['state'] == 'cancelled' else 'Starting Now' if stage == 'start' else 'Scheduled'
        color = 0xC74949 if event['state'] == 'cancelled' else 0xE5B75E if stage in ('1h','15m','start') else 0x318BCE
        description = clean(event['notes'], 1400) or 'Check the in-game Events calendar for the event details.'
        result = discord.Embed(title=f"SaS | {name}", description=description, color=color)
        result.add_field(name='EVENT TYPE', value=clean(event['category'], 60), inline=True)
        result.add_field(name='SERVER START', value=f"<t:{event['starts_at']}:F>", inline=True)
        result.add_field(name='COUNTDOWN', value=f"<t:{event['starts_at']}:R>", inline=True)
        result.add_field(name='STATUS', value=status, inline=True)
        result.add_field(name='REMINDERS', value='24h / 1h / 15m / Start\nPersonal DM: 1h and 15m, opt-in only.', inline=False)
        result.set_footer(text=f"SaS Event Center | Event #{event['id']} | {stage}")
        if event['banner_url'] and stage in ('upcoming','cancelled'):
            result.set_image(url=event['banner_url'])
        return result

    async def upcoming_message(self, interaction):
        rows = self.store.upcoming(interaction.guild_id)
        result = discord.Embed(title='SaS | Upcoming Events', color=0x318BCE)
        if rows:
            for event in rows[:10]:
                result.add_field(
                    name=f"#{event['id']} | {clean(event['title'], 90)}",
                    value=f"{clean(event['category'], 50)} | <t:{event['starts_at']}:F> (<t:{event['starts_at']}:R>)",
                    inline=False
                )
        else:
            result.description = 'No upcoming events have been scheduled.'
        await interaction.response.send_message(embed=result, ephemeral=True)

    def register(self):
        if self.group_registered:
            return
        group = app_commands.Group(name='event', description='SaS event calendar and reminders')

        @group.command(name='setup', description='Choose the reminder channel and optional role ping')
        async def setup(interaction: discord.Interaction, channel: discord.TextChannel, notify_role: discord.Role = None):
            if not self.is_staff(interaction):
                return await interaction.response.send_message('Staff access required.', ephemeral=True)
            permissions = channel.permissions_for(interaction.guild.me)
            if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.attach_files, permissions.read_message_history)):
                return await interaction.response.send_message('The bot needs View Channel, Send Messages, Embed Links, Attach Files and Read Message History.', ephemeral=True)
            self.store.set_channel(interaction.guild_id, channel.id, notify_role.id if notify_role else None)
            await interaction.response.send_message(f"Event reminders will be posted in {channel.mention}. Role mention at event start: {notify_role.mention if notify_role else 'disabled'}.", ephemeral=True)

        @group.command(name='create', description='Schedule an event using the in-game server time in UTC')
        @app_commands.describe(title='Event name', start_utc='Game server time: YYYY-MM-DD HH:MM UTC', category='Alliance, Events, Deals, or Other', notes='Optional event details')
        async def create(interaction: discord.Interaction, title: str, start_utc: str, category: Literal['Alliance','Events','Deals','Other'] = 'Alliance', notes: str = ''):
            if not self.is_staff(interaction):
                return await interaction.response.send_message('Staff access required.', ephemeral=True)
            settings = self.store.settings(interaction.guild_id)
            if settings is None:
                return await interaction.response.send_message('Run /event setup first.', ephemeral=True)
            try:
                starts_at = parse_utc(start_utc)
            except ValueError as error:
                return await interaction.response.send_message(str(error), ephemeral=True)
            title = title.strip()
            notes = notes.strip()
            if len(title) < 3 or len(title) > 100 or len(notes) > 900 or category not in CATEGORIES:
                return await interaction.response.send_message('Use a 3–100 character title, at most 900 characters of notes and a valid category.', ephemeral=True)
            channel = interaction.guild.get_channel(settings['channel_id'])
            if not isinstance(channel, discord.TextChannel):
                return await interaction.response.send_message('The configured reminder channel no longer exists.', ephemeral=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            now = int(datetime.now(timezone.utc).timestamp())
            event_id = self.store.create(interaction.guild_id, channel.id, title, category, notes, starts_at, interaction.user.id, now)
            event = self.store.event(event_id)
            embed = self.build_embed(event)
            kwargs = dict(embed=embed, view=EventReminderView(self), allowed_mentions=discord.AllowedMentions.none())
            try:
                kwargs['file'] = discord.File(event_banner(title, category, 'upcoming'), filename='sas_event.png')
                embed.set_image(url='attachment://sas_event.png')
            except Exception:
                log.exception('Unable to render event banner for %s', event_id)
            try:
                message = await channel.send(**kwargs)
            except discord.HTTPException:
                log.exception('Could not publish SaS event %s', event_id)
                self.store.cancel(event_id, interaction.guild_id)
                return await interaction.followup.send('The event could not be published. Check channel permissions.', ephemeral=True)
            banner_url = message.attachments[0].url if message.attachments else None
            self.store.set_message(event_id, message.id, banner_url)
            await interaction.followup.send(f"Scheduled **{clean(title, 100)}** in {channel.mention} for <t:{starts_at}:F>. Event ID: {event_id}.", ephemeral=True)

        @group.command(name='upcoming', description='See the next scheduled SaS events')
        async def upcoming(interaction: discord.Interaction):
            if interaction.guild_id != getattr(self.bot.guild, 'id', None):
                return await interaction.response.send_message('Only available in the SaS server.', ephemeral=True)
            await self.upcoming_message(interaction)

        @group.command(name='cancel', description='Cancel a scheduled event and stop future notifications')
        async def cancel(interaction: discord.Interaction, event_id: int):
            if not self.is_staff(interaction):
                return await interaction.response.send_message('Staff access required.', ephemeral=True)
            if not self.store.cancel(event_id, interaction.guild_id):
                return await interaction.response.send_message('Active event not found.', ephemeral=True)
            event = self.store.event(event_id)
            channel = interaction.guild.get_channel(event['channel_id'])
            if channel and event['message_id']:
                try:
                    message = await channel.fetch_message(event['message_id'])
                    await message.edit(embed=self.build_embed(event, 'cancelled'), view=None)
                except discord.HTTPException:
                    log.exception('Could not update cancelled event %s', event_id)
            await interaction.response.send_message(f'Event #{event_id} cancelled. Scheduled notifications were stopped.', ephemeral=True)

        self.bot.tree.add_command(group, guild=self.bot.guild)
        self.group_registered = True

    @tasks.loop(seconds=30)
    async def scheduler(self):
        if not self.bot.configured:
            return
        now = int(datetime.now(timezone.utc).timestamp())
        for reminder in self.store.due(now):
            event = self.store.event(reminder['event_id'])
            stage = reminder['stage']
            if event['state'] != 'active' or now - reminder['due_at'] > 600:
                self.store.reminder_state(event['id'], stage, 'skipped')
                continue
            channel = self.bot.guild.get_channel(event['channel_id'])
            if not isinstance(channel, discord.TextChannel):
                continue
            result = self.build_embed(event, stage)
            extra = {}
            try:
                extra['file'] = discord.File(event_banner(event['title'], event['category'], stage), filename='sas_event_reminder.png')
                result.set_image(url='attachment://sas_event_reminder.png')
            except Exception:
                log.exception('Unable to render reminder banner for event %s', event['id'])
            settings = self.store.settings(event['guild_id'])
            role = self.bot.guild.get_role(settings['role_id']) if settings and settings['role_id'] and stage == 'start' else None
            try:
                message = await channel.send(
                    content=role.mention if role else None,
                    embed=result,
                    allowed_mentions=discord.AllowedMentions(roles=[role] if role else [], users=False, everyone=False),
                    **extra
                )
            except discord.HTTPException:
                log.exception('Failed to send event %s stage %s',event['id'],stage)
                continue
            self.store.reminder_state(event['id'],stage,'sent',message.id)
            if stage == 'start' and event['message_id']:
                try:
                    original = await channel.fetch_message(event['message_id'])
                    await original.edit(embed=self.build_embed(event, 'start'), view=None)
                except discord.HTTPException:
                    log.exception('Could not close event panel %s',event['id'])
        for job in self.store.due_dm():
            event = self.store.event(job['event_id'])
            if event['state'] != 'active':
                self.store.dm_state(job['event_id'], job['stage'], job['user_id'], 'skipped')
                continue
            if int(datetime.now(timezone.utc).timestamp()) >= event['starts_at'] + 300:
                self.store.dm_state(job['event_id'], job['stage'], job['user_id'], 'skipped')
                continue
            message = self.build_embed(event,job['stage'])
            try:
                user = await self.bot.fetch_user(job['user_id'])
                await user.send(embed=message)
            except (discord.Forbidden,discord.NotFound):
                self.store.dm_state(job['event_id'],job['stage'],job['user_id'],'blocked')
            except discord.HTTPException:
                log.exception('Could not send event DM for event %s',event['id'])
            else:
                self.store.dm_state(job['event_id'],job['stage'],job['user_id'],'sent')

    @scheduler.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.scheduler.is_running():
            self.scheduler.start()

    def close(self):
        if self.scheduler.is_running():
            self.scheduler.cancel()
        self.store.close()
