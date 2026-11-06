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

from event_graphics import event_banner, weekly_calendar


log = logging.getLogger('sas.events')
STAGES = (('24h', 86400), ('1h', 3600), ('15m', 900), ('start', 0))
CATEGORIES = ('Alliance', 'Events', 'Deals', 'Port Inspection', 'Other')


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
                role_id INTEGER,
                calendar_message_id INTEGER,
                calendar_refreshed_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS game_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                notes TEXT NOT NULL,
                starts_at INTEGER NOT NULL,
                ends_at INTEGER,
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
        settings_columns = {row['name'] for row in self.conn.execute('PRAGMA table_info(event_settings)')}
        if 'calendar_message_id' not in settings_columns:
            self.conn.execute('ALTER TABLE event_settings ADD COLUMN calendar_message_id INTEGER')
        if 'calendar_refreshed_at' not in settings_columns:
            self.conn.execute('ALTER TABLE event_settings ADD COLUMN calendar_refreshed_at INTEGER')
        columns = {row['name'] for row in self.conn.execute('PRAGMA table_info(game_events)')}
        if 'ends_at' not in columns:
            self.conn.execute('ALTER TABLE game_events ADD COLUMN ends_at INTEGER')
        self.conn.execute('UPDATE game_events SET ends_at=starts_at+3600 WHERE ends_at IS NULL')

    def set_channel(self, guild_id, channel_id, role_id):
        self.conn.execute(
            'INSERT INTO event_settings(guild_id,channel_id,role_id) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,role_id=excluded.role_id,calendar_message_id=CASE WHEN channel_id=excluded.channel_id THEN calendar_message_id ELSE NULL END,calendar_refreshed_at=CASE WHEN channel_id=excluded.channel_id THEN calendar_refreshed_at ELSE NULL END',
            (guild_id, channel_id, role_id)
        )

    def settings(self, guild_id):
        return self.conn.execute('SELECT * FROM event_settings WHERE guild_id=?', (guild_id,)).fetchone()

    def set_board(self, guild_id, message_id, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        self.conn.execute(
            'UPDATE event_settings SET calendar_message_id=?,calendar_refreshed_at=? WHERE guild_id=?',
            (message_id, now, guild_id)
        )

    def boards_to_refresh(self, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        return self.conn.execute(
            'SELECT * FROM event_settings WHERE calendar_message_id IS NOT NULL AND COALESCE(calendar_refreshed_at,0)<=?',
            (now - 3600,)
        ).fetchall()

    def create(self, guild_id, channel_id, title, category, notes, starts_at, author_id, now=None, ends_at=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        ends_at = starts_at + 3600 if ends_at is None else ends_at
        if ends_at <= starts_at or ends_at - starts_at > 14 * 86400:
            raise ValueError('Event end must be after start and within 14 days.')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            cursor = self.conn.execute(
                'INSERT INTO game_events(guild_id,channel_id,title,category,notes,starts_at,ends_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)',
                (guild_id, channel_id, title, category, notes, starts_at, ends_at, now, author_id)
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

    def edit(self, event_id, guild_id, title, category, notes, starts_at, ends_at, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        if starts_at < now + 300 or ends_at <= starts_at or ends_at - starts_at > 14 * 86400:
            raise ValueError('Event must begin at least 5 minutes ahead and last no longer than 14 days.')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            original = self.conn.execute(
                "SELECT * FROM game_events WHERE id=? AND guild_id=? AND state='active'",
                (event_id, guild_id)
            ).fetchone()
            if original is None:
                self.conn.execute('COMMIT')
                return False
            changed_time = original['starts_at'] != starts_at
            self.conn.execute(
                'UPDATE game_events SET title=?,category=?,notes=?,starts_at=?,ends_at=? WHERE id=?',
                (title, category, notes, starts_at, ends_at, event_id)
            )
            if changed_time:
                self.conn.execute('DELETE FROM event_reminders WHERE event_id=?', (event_id,))
                self.conn.execute('DELETE FROM event_dm_jobs WHERE event_id=?', (event_id,))
                for stage, seconds in STAGES:
                    due_at = starts_at - seconds
                    if due_at >= now - 60:
                        self.conn.execute(
                            'INSERT INTO event_reminders(event_id,stage,due_at) VALUES(?,?,?)',
                            (event_id, stage, due_at)
                        )
            self.conn.execute('COMMIT')
            return True
        except Exception:
            self.conn.execute('ROLLBACK')
            raise

    def calendar_events(self, guild_id, first_day, last_day):
        return self.conn.execute(
            "SELECT * FROM game_events WHERE guild_id=? AND state='active' AND starts_at<? AND ends_at>? ORDER BY starts_at LIMIT 80",
            (guild_id, last_day, first_day)
        ).fetchall()

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


def parse_utc(value, now=None, future=True):
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    start = None
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y:%m:%d %H:%M:%S', '%Y:%m:%d %H:%M'):
        try:
            start = datetime.strptime(value.strip(), pattern).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if start is None:
        raise ValueError('Enter the exact game/server time in UTC as YYYY-MM-DD HH:MM:SS, for example 2026-11-08 23:59:59.')
    timestamp = int(start.timestamp())
    if future and (timestamp < now + 300 or timestamp > now + 90 * 86400):
        raise ValueError('Event starts must be between 5 minutes and 90 days from now.')
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
        await self.manager.show_calendar(interaction)


class CalendarNavigation(discord.ui.View):
    def __init__(self, manager, user_id, week_offset=0):
        super().__init__(timeout=240)
        self.manager = manager
        self.user_id = user_id
        self.week_offset = week_offset

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Open your own calendar with /event calendar.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Previous Week', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.week_offset = max(-12, self.week_offset - 1)
        await self.manager.show_calendar(interaction, self.week_offset, update=True, view=self)

    @discord.ui.button(label='This Week', style=discord.ButtonStyle.primary)
    async def current(self, interaction, button):
        self.week_offset = 0
        await self.manager.show_calendar(interaction, self.week_offset, update=True, view=self)

    @discord.ui.button(label='Next Week', style=discord.ButtonStyle.secondary)
    async def next_week(self, interaction, button):
        self.week_offset = min(12, self.week_offset + 1)
        await self.manager.show_calendar(interaction, self.week_offset, update=True, view=self)

    @discord.ui.button(label='Upcoming Events', style=discord.ButtonStyle.secondary)
    async def upcoming(self, interaction, button):
        rows = self.manager.store.upcoming(interaction.guild_id)
        result = self.manager.upcoming_embed(rows)
        await interaction.response.send_message(embed=result, ephemeral=True)


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
        result.add_field(name='SERVER END', value=f"<t:{event['ends_at']}:F>", inline=True)
        result.add_field(name='COUNTDOWN', value=f"<t:{event['starts_at']}:R>", inline=True)
        server_start = datetime.fromtimestamp(event['starts_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        server_end = datetime.fromtimestamp(event['ends_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        result.add_field(name='IN-GAME SERVER TIME', value=f"{server_start} - {server_end}", inline=False)
        result.add_field(name='STATUS', value=status, inline=True)
        result.add_field(name='REMINDERS', value='24h / 1h / 15m / Start\nPersonal DM: 1h and 15m, opt-in only.', inline=False)
        result.set_footer(text=f"SaS Event Center | Event #{event['id']} | {stage}")
        if event['banner_url'] and stage in ('upcoming','cancelled'):
            result.set_image(url=event['banner_url'])
        return result

    def upcoming_embed(self, rows):
        result = discord.Embed(title='SaS | Upcoming Events', color=0x318BCE)
        if rows:
            for event in rows[:10]:
                result.add_field(
                    name=f"#{event['id']} | {clean(event['title'], 90)}",
                    value=f"{clean(event['category'], 50)} | <t:{event['starts_at']}:F> - <t:{event['ends_at']}:F> (<t:{event['starts_at']}:R>)",
                    inline=False
                )
        else:
            result.description = 'No upcoming events have been scheduled.'
        return result

    async def upcoming_message(self, interaction):
        await interaction.response.send_message(embed=self.upcoming_embed(self.store.upcoming(interaction.guild_id)), ephemeral=True)

    def calendar_payload(self, guild_id, week_offset=0):
        now = datetime.now(timezone.utc)
        first = now.date() - timedelta(days=2) + timedelta(days=week_offset * 7)
        begin = int(datetime.combine(first, datetime.min.time(), timezone.utc).timestamp())
        end = begin + 7 * 86400
        events = self.store.calendar_events(guild_id, begin, end)
        banner = discord.File(weekly_calendar(events, first, now, 'UTC'), filename='sas_weekly_calendar.png')
        embed = discord.Embed(
            title='SaS | Weekly Event Calendar',
            description=f"**Server Time:** {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n**Calendar:** {first.strftime('%d %b')} - {(first + timedelta(days=6)).strftime('%d %b %Y')}\n**Scheduled events:** {len(events)}",
            color=0x67B0E1
        )
        embed.set_image(url='attachment://sas_weekly_calendar.png')
        details = []
        for event in events[:8]:
            starts = datetime.fromtimestamp(event['starts_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            ends = datetime.fromtimestamp(event['ends_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            details.append(f"**#{event['id']} {clean(event['title'], 45)}** | {starts} - {ends} UTC")
        if details:
            embed.add_field(name='EVENT TIMES | SERVER CLOCK', value='\n'.join(details)[:1024], inline=False)
        embed.set_footer(text='SaS Event Center | Times follow the in-game server schedule | UTC')
        embed.timestamp = now
        return embed, banner

    async def refresh_board(self, guild_id):
        settings = self.store.settings(guild_id)
        if settings is None or not settings['calendar_message_id']:
            return None
        channel = self.bot.guild.get_channel(settings['channel_id'])
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            message = await channel.fetch_message(settings['calendar_message_id'])
        except discord.NotFound:
            self.store.set_board(guild_id, None)
            return None
        embed, banner = self.calendar_payload(guild_id)
        edited = await message.edit(embed=embed, attachments=[banner], allowed_mentions=discord.AllowedMentions.none())
        self.store.set_board(guild_id, edited.id if edited else message.id)
        return edited or message

    async def refresh_board_safe(self, guild_id):
        try:
            await self.refresh_board(guild_id)
        except Exception:
            log.exception('Could not refresh public event calendar in server %s', guild_id)

    async def show_calendar(self, interaction, week_offset=0, update=False, view=None, publish=False):
        if interaction.guild_id != getattr(self.bot.guild, 'id', None):
            return await interaction.response.send_message('Only available in the SaS server.', ephemeral=True)
        if publish:
            settings = self.store.settings(interaction.guild_id)
            if settings is None:
                return await interaction.response.send_message('Run /event setup first.', ephemeral=True)
            channel = interaction.guild.get_channel(settings['channel_id'])
            if not isinstance(channel, discord.TextChannel):
                return await interaction.response.send_message('Configured calendar channel is unavailable.', ephemeral=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                existing = await self.refresh_board(interaction.guild_id)
                if existing:
                    return await interaction.followup.send(f'Calendar refreshed in {channel.mention}: {existing.jump_url}', ephemeral=True)
                embed, banner = self.calendar_payload(interaction.guild_id, 0)
                message = await channel.send(embed=embed, file=banner, allowed_mentions=discord.AllowedMentions.none())
                self.store.set_board(interaction.guild_id, message.id)
            except discord.HTTPException:
                log.exception('Unable to publish SaS weekly calendar')
                return await interaction.followup.send('Calendar could not be posted. Check channel permissions.', ephemeral=True)
            return await interaction.followup.send(f'Calendar posted in {channel.mention}: {message.jump_url}', ephemeral=True)
        embed, banner = self.calendar_payload(interaction.guild_id, week_offset)
        if view is None:
            view = CalendarNavigation(self, interaction.user.id, week_offset)
        if update:
            await interaction.response.edit_message(embed=embed, attachments=[banner], view=view)
        else:
            await interaction.response.send_message(embed=embed, file=banner, view=view, ephemeral=True)

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
        @app_commands.describe(title='Event name', start_utc='Start YYYY-MM-DD HH:MM:SS UTC', category='Event color category', notes='Optional event details', end_utc='Optional end YYYY-MM-DD HH:MM:SS UTC')
        async def create(interaction: discord.Interaction, title: str, start_utc: str, category: Literal['Alliance','Events','Deals','Port Inspection','Other'] = 'Alliance', notes: str = '', end_utc: str = ''):
            if not self.is_staff(interaction):
                return await interaction.response.send_message('Staff access required.', ephemeral=True)
            settings = self.store.settings(interaction.guild_id)
            if settings is None:
                return await interaction.response.send_message('Run /event setup first.', ephemeral=True)
            try:
                starts_at = parse_utc(start_utc)
                ends_at = parse_utc(end_utc, future=False) if end_utc.strip() else starts_at + 3600
                if ends_at <= starts_at or ends_at - starts_at > 14 * 86400:
                    raise ValueError('End time must follow the start and be within 14 days.')
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
            event_id = self.store.create(interaction.guild_id, channel.id, title, category, notes, starts_at, interaction.user.id, now, ends_at)
            event = self.store.event(event_id)
            embed = self.build_embed(event)
            kwargs = dict(embed=embed, view=EventReminderView(self), allowed_mentions=discord.AllowedMentions.none())
            try:
                kwargs['file'] = discord.File(event_banner(title, category, 'upcoming', starts_at, ends_at), filename='sas_event.png')
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
            await self.refresh_board_safe(interaction.guild_id)
            await interaction.followup.send(f"Scheduled **{clean(title, 100)}** in {channel.mention} from <t:{starts_at}:F> to <t:{ends_at}:F>. Event ID: {event_id}.", ephemeral=True)

        @group.command(name='calendar', description='View a game-style seven-day calendar with server times')
        @app_commands.describe(week_offset='Weeks backward or forward (-12 to +12)', publish='Staff: post calendar to the event channel')
        async def calendar(interaction: discord.Interaction, week_offset: app_commands.Range[int,-12,12] = 0, publish: bool = False):
            if publish and not self.is_staff(interaction):
                return await interaction.response.send_message('Only staff can publish the calendar.', ephemeral=True)
            await self.show_calendar(interaction, week_offset, publish=publish)

        @group.command(name='edit', description='Edit or reschedule an existing event')
        @app_commands.describe(event_id='Event number from /event upcoming', title='New title if changing', start_utc='Start YYYY-MM-DD HH:MM:SS UTC', end_utc='End YYYY-MM-DD HH:MM:SS UTC', category='New category if changing', notes='New event details if changing')
        async def edit(interaction: discord.Interaction, event_id: int, title: str = '', start_utc: str = '', end_utc: str = '', category: str = '', notes: str = ''):
            if not self.is_staff(interaction):
                return await interaction.response.send_message('Staff access required.', ephemeral=True)
            original = self.store.event(event_id)
            if original is None or original['guild_id'] != interaction.guild_id or original['state'] != 'active':
                return await interaction.response.send_message('Active event not found.', ephemeral=True)
            new_title = title.strip() or original['title']
            new_category = category.strip() or original['category']
            new_notes = notes.strip() or original['notes']
            try:
                starts_at = parse_utc(start_utc) if start_utc.strip() else original['starts_at']
                ends_at = parse_utc(end_utc, future=False) if end_utc.strip() else (starts_at + original['ends_at'] - original['starts_at'] if start_utc.strip() else original['ends_at'])
                if starts_at < int(datetime.now(timezone.utc).timestamp()) + 300:
                    raise ValueError('You can only edit events starting at least 5 minutes from now.')
                if len(new_title) < 3 or len(new_title) > 100 or len(new_notes) > 900 or new_category not in CATEGORIES:
                    raise ValueError('Invalid title, category or notes.')
                if not self.store.edit(event_id, interaction.guild_id, new_title, new_category, new_notes, starts_at, ends_at):
                    raise ValueError('Event no longer available.')
            except ValueError as error:
                return await interaction.response.send_message(str(error), ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            updated = self.store.event(event_id)
            channel = interaction.guild.get_channel(updated['channel_id'])
            if isinstance(channel, discord.TextChannel) and updated['message_id']:
                try:
                    message = await channel.fetch_message(updated['message_id'])
                    embed = self.build_embed(updated)
                    attachments = []
                    try:
                        attachments = [discord.File(event_banner(updated['title'], updated['category'], 'upcoming', updated['starts_at'], updated['ends_at']), filename='sas_event.png')]
                        embed.set_image(url='attachment://sas_event.png')
                    except Exception:
                        log.exception('Unable to update banner for event %s', event_id)
                    edited = await message.edit(embed=embed, attachments=attachments, view=EventReminderView(self))
                    if edited and edited.attachments:
                        self.store.set_message(event_id, edited.id, edited.attachments[0].url)
                except discord.HTTPException:
                    log.exception('Unable to update existing event message %s',event_id)
            await self.refresh_board_safe(interaction.guild_id)
            await interaction.followup.send(f'Event #{event_id} updated. Reminders follow the new start time if rescheduled.', ephemeral=True)

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
            await self.refresh_board_safe(interaction.guild_id)
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
                extra['file'] = discord.File(event_banner(event['title'], event['category'], stage, event['starts_at'], event['ends_at']), filename='sas_event_reminder.png')
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
        for settings in self.store.boards_to_refresh(now):
            await self.refresh_board_safe(settings['guild_id'])
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
