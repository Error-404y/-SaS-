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

from event_graphics import weekly_calendar, event_board


log = logging.getLogger('sas.events')
DEFAULT_EVENT_BOARD_CHANNEL_ID = 1568424573836464208
DEFAULT_EVENT_TEXT_CHANNEL_ID = 1568431370747908291
DEFAULT_EVENT_MEMBER_ROLE_ID = 1562535352290250842
STAGES = (('1h', 3600),)
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
                calendar_refreshed_at INTEGER,
                calendar_message_channel_id INTEGER,
                text_message_id INTEGER,
                text_message_channel_id INTEGER,
                text_refreshed_at INTEGER
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
                state TEXT NOT NULL DEFAULT 'active',
                display_phase TEXT
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
        if 'calendar_message_channel_id' not in settings_columns:
            self.conn.execute('ALTER TABLE event_settings ADD COLUMN calendar_message_channel_id INTEGER')
        self.conn.execute('UPDATE event_settings SET calendar_message_channel_id=channel_id WHERE calendar_message_id IS NOT NULL AND calendar_message_channel_id IS NULL')
        for name in ('text_message_id', 'text_message_channel_id', 'text_refreshed_at'):
            if name not in settings_columns:
                self.conn.execute(f'ALTER TABLE event_settings ADD COLUMN {name} INTEGER')
        columns = {row['name'] for row in self.conn.execute('PRAGMA table_info(game_events)')}
        if 'ends_at' not in columns:
            self.conn.execute('ALTER TABLE game_events ADD COLUMN ends_at INTEGER')
        if 'display_phase' not in columns:
            self.conn.execute('ALTER TABLE game_events ADD COLUMN display_phase TEXT')
        self.conn.execute('UPDATE game_events SET ends_at=starts_at+3600 WHERE ends_at IS NULL')
        self.conn.execute("UPDATE event_reminders SET state='skipped' WHERE state='pending' AND stage!='1h'")
        self.conn.execute("UPDATE event_dm_jobs SET state='skipped' WHERE state='pending'")

    def set_channel(self, guild_id, channel_id, role_id):
        self.conn.execute(
            'INSERT INTO event_settings(guild_id,channel_id,role_id) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,role_id=excluded.role_id',
            (guild_id, channel_id, role_id)
        )

    def settings(self, guild_id):
        return self.conn.execute('SELECT * FROM event_settings WHERE guild_id=?', (guild_id,)).fetchone()

    def set_board(self, guild_id, message_id, now=None, channel_id=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        if message_id is None:
            channel_id = None
        elif channel_id is None:
            previous = self.settings(guild_id)
            channel_id = previous['calendar_message_channel_id'] or previous['channel_id']
        self.conn.execute(
            'UPDATE event_settings SET calendar_message_id=?,calendar_refreshed_at=?,calendar_message_channel_id=? WHERE guild_id=?',
            (message_id, now, channel_id, guild_id)
        )

    def boards_to_refresh(self, now=None, target_channel_id=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        if target_channel_id is None:
            return self.conn.execute(
                'SELECT * FROM event_settings WHERE calendar_message_id IS NOT NULL AND COALESCE(calendar_refreshed_at,0)<=?',
                (now - 3600,)
            ).fetchall()
        return self.conn.execute(
            'SELECT * FROM event_settings WHERE calendar_message_id IS NOT NULL AND (COALESCE(calendar_refreshed_at,0)<=? OR COALESCE(calendar_message_channel_id,channel_id)!=?)',
            (now - 3600, target_channel_id)
        ).fetchall()

    def set_text_message(self, guild_id, message_id, channel_id, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        self.conn.execute(
            'UPDATE event_settings SET text_message_id=?,text_message_channel_id=?,text_refreshed_at=? WHERE guild_id=?',
            (message_id, channel_id if message_id else None, now, guild_id)
        )

    def text_feeds_to_refresh(self, now=None, target_channel_id=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        return self.conn.execute(
            """SELECT * FROM event_settings
            WHERE text_message_id IS NOT NULL
            AND (COALESCE(text_refreshed_at,0)<=? OR text_message_channel_id!=?
            OR EXISTS (
                SELECT 1 FROM game_events e WHERE e.guild_id=event_settings.guild_id
                AND e.state='active'
                AND ((e.starts_at>COALESCE(text_refreshed_at,0) AND e.starts_at<=?)
                  OR (e.ends_at>COALESCE(text_refreshed_at,0) AND e.ends_at<=?))
            ))""",
            (now - 3600, target_channel_id, now, now)
        ).fetchall()

    def legacy_event_cards(self, guild_id, limit=12):
        return self.conn.execute(
            'SELECT * FROM game_events WHERE guild_id=? AND message_id IS NOT NULL ORDER BY id LIMIT ?',
            (guild_id, limit)
        ).fetchall()

    def create(self, guild_id, channel_id, title, category, notes, starts_at, author_id, now=None, ends_at=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        ends_at = starts_at + 3600 if ends_at is None else ends_at
        validate_event_window(starts_at, ends_at, now)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            cursor = self.conn.execute(
                'INSERT INTO game_events(guild_id,channel_id,title,category,notes,starts_at,ends_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)',
                (guild_id, channel_id, title, category, notes, starts_at, ends_at, now, author_id)
            )
            event_id = cursor.lastrowid
            for stage, seconds in STAGES:
                due_at = starts_at - seconds
                if due_at > now:
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
        validate_event_window(starts_at, ends_at, now)
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
                    if due_at > now:
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
        event = self.event(event_id)
        phase = event_phase(event) if event else None
        self.conn.execute(
            'UPDATE game_events SET message_id=?,banner_url=?,display_phase=? WHERE id=?',
            (message_id, banner_url, phase, event_id)
        )

    def mark_display_phase(self, event_id, phase):
        self.conn.execute('UPDATE game_events SET display_phase=?,banner_url=NULL WHERE id=?', (phase, event_id))

    def mark_card_missing(self, event_id):
        self.conn.execute('UPDATE game_events SET message_id=NULL,banner_url=NULL,display_phase=NULL WHERE id=?', (event_id,))

    def cards_to_refresh(self, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        return self.conn.execute(
            """SELECT * FROM game_events WHERE message_id IS NOT NULL
            AND COALESCE(display_phase,'') != CASE
                WHEN state='cancelled' THEN 'Cancelled'
                WHEN ends_at<=? THEN 'Completed'
                WHEN starts_at<=? THEN 'In Progress'
                ELSE 'Upcoming' END
            ORDER BY starts_at LIMIT 20""",
            (now, now)
        ).fetchall()

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
            "SELECT * FROM game_events WHERE guild_id=? AND state='active' AND ends_at>? ORDER BY starts_at LIMIT 15",
            (guild_id, now)
        ).fetchall()

    def board_events(self, guild_id, now=None, limit=24):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        return self.conn.execute(
            """SELECT * FROM game_events WHERE guild_id=? AND state='active' AND ends_at>?
            ORDER BY CASE WHEN starts_at<=? THEN 0 ELSE 1 END, starts_at, id LIMIT ?""",
            (guild_id, now, now, limit)
        ).fetchall()

    def board_event_count(self, guild_id, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        return self.conn.execute(
            "SELECT COUNT(*) FROM game_events WHERE guild_id=? AND state='active' AND ends_at>?",
            (guild_id, now)
        ).fetchone()[0]

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


def validate_event_window(starts_at, ends_at, now):
    if ends_at <= starts_at or ends_at - starts_at > 14 * 86400:
        raise ValueError('Event end must be after the start and within 14 days.')
    if ends_at <= now:
        raise ValueError('This event has already ended. Its end time must still be in the future.')
    if starts_at > now + 90 * 86400:
        raise ValueError('Events cannot start more than 90 days from now.')
    if now < starts_at < now + 300:
        raise ValueError('Future events must start at least 5 minutes from now.')


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


def event_phase(event, now=None):
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    if event['state'] == 'cancelled':
        return 'Cancelled'
    if now >= event['ends_at']:
        return 'Completed'
    if now >= event['starts_at']:
        return 'In Progress'
    return 'Upcoming'


def clean(value, limit):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))[:limit]


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
        self.board_lock = asyncio.Lock()
        self.text_lock = asyncio.Lock()
        self.board_channel_id = int(os.getenv('EVENT_BOARD_CHANNEL_ID', str(DEFAULT_EVENT_BOARD_CHANNEL_ID)))
        self.text_channel_id = int(os.getenv('EVENT_TEXT_CHANNEL_ID', str(DEFAULT_EVENT_TEXT_CHANNEL_ID)))
        self.member_role_id = int(os.getenv('EVENT_MEMBER_ROLE_ID', str(DEFAULT_EVENT_MEMBER_ROLE_ID)))

    def is_staff(self, interaction):
        return bool(
            self.bot.configured and self.bot.guild and interaction.guild_id == self.bot.guild.id
            and isinstance(interaction.user, discord.Member) and self.bot.is_reviewer(interaction.user)
        )

    def build_embed(self, event, stage=None, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        phase = event_phase(event, now)
        title = clean(event['title'], 200)
        description = clean(event['notes'], 1400) or 'See the in-game Events calendar for further information.'
        color = {
            'Upcoming': 0x318BCE,
            'In Progress': 0xE5B75E,
            'Completed': 0x738499,
            'Cancelled': 0xC74949
        }[phase]
        heading = f"SaS | {title}"
        stage_names = {'24h': '24-Hour Reminder', '1h': '1-Hour Reminder', '15m': '15-Minute Reminder', 'start': 'Event Start'}
        if stage in stage_names:
            heading = f"SaS | {stage_names[stage]} | {title}"
        result = discord.Embed(title=heading, description=description, color=color)
        result.add_field(name='EVENT TYPE', value=clean(event['category'], 60), inline=True)
        result.add_field(name='STATUS', value=phase, inline=True)
        result.add_field(name='SERVER START', value=f"<t:{event['starts_at']}:F>", inline=True)
        result.add_field(name='SERVER END', value=f"<t:{event['ends_at']}:F>", inline=True)
        if phase == 'Upcoming':
            result.add_field(name='STARTS IN', value=f"<t:{event['starts_at']}:R>", inline=True)
        elif phase == 'In Progress':
            result.add_field(name='ENDS IN', value=f"<t:{event['ends_at']}:R>", inline=True)
        server_start = datetime.fromtimestamp(event['starts_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        server_end = datetime.fromtimestamp(event['ends_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        result.add_field(name='IN-GAME SERVER TIME (UTC)', value=f"{server_start} — {server_end}", inline=False)
        reminders = {
            'Upcoming': 'Channel: 24h, 1h, 15m, start. Opt-in DMs: 1h and 15m.',
            'In Progress': 'The event has started. No earlier reminders will be sent.',
            'Completed': 'The event has ended. All reminders are closed.',
            'Cancelled': 'This event was cancelled. All reminders are disabled.'
        }
        result.add_field(name='REMINDERS', value=reminders[phase], inline=False)
        result.set_footer(text=f"SaS Event Center | Event #{event['id']} | {phase}")
        return result

    async def sync_event_card(self, event, message=None):
        if not event['message_id']:
            return None
        channel = self.bot.guild.get_channel(event['channel_id'])
        if not isinstance(channel, discord.TextChannel):
            return None
        if message is None:
            message = await channel.fetch_message(event['message_id'])
        phase = event_phase(event)
        view = EventReminderView(self) if phase == 'Upcoming' else None
        await message.edit(embed=self.build_embed(event), attachments=[], view=view, allowed_mentions=discord.AllowedMentions.none())
        self.store.mark_display_phase(event['id'], phase)
        return message

    def upcoming_embed(self, rows):
        result = discord.Embed(title='SaS | Upcoming Events', color=0x318BCE)
        if rows:
            for event in rows[:10]:
                result.add_field(
                    name=f"#{event['id']} | {clean(event['title'], 90)}",
                    value=f"{clean(event['category'], 50)} | **{event_phase(event)}** | <t:{event['starts_at']}:F> — <t:{event['ends_at']}:F> | <t:{event['ends_at'] if event_phase(event) == 'In Progress' else event['starts_at']}:R>",
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

    def board_payload(self, guild_id):
        now = datetime.now(timezone.utc)
        records = self.store.board_events(guild_id, int(now.timestamp()))
        entries = [
            {
                'id': item['id'], 'title': item['title'], 'category': item['category'],
                'starts_at': item['starts_at'], 'ends_at': item['ends_at'],
                'phase': event_phase(item, int(now.timestamp()))
            }
            for item in records
        ]
        total = self.store.board_event_count(guild_id, int(now.timestamp()))
        file = discord.File(event_board(entries, now, total), filename='sas_events_overview.png')
        embed = discord.Embed(
            title='SaS | Event Center',
            description=f"**Server Time:** {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n**Active and upcoming events:** {total}\nNew events are automatically added to this graphic. Use /event calendar to browse the seven-day timeline.",
            color=0x5DAAE3
        )
        embed.set_image(url='attachment://sas_events_overview.png')
        embed.set_footer(text='SaS Event Center | Shared schedule | All times UTC')
        embed.timestamp = now
        return embed, file

    def text_channel(self):
        channel = self.bot.guild.get_channel(self.text_channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.id != self.text_channel_id:
            return None
        return channel

    def text_payload(self, guild_id, now=None):
        now = int(datetime.now(timezone.utc).timestamp()) if now is None else now
        items = self.store.board_events(guild_id, now=now, limit=14)
        total = self.store.board_event_count(guild_id, now=now)
        embed = discord.Embed(
            title='SaS | Event Updates',
            description=f'**Active and upcoming events:** {total}\nThis is the live event list. New events are added here automatically.',
            color=discord.Color.from_rgb(93, 170, 227)
        )
        for event in items:
            phase = event_phase(event, now)
            start = datetime.fromtimestamp(event['starts_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            end = datetime.fromtimestamp(event['ends_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            name = f"#{event['id']} | {clean(event['title'], 80)} | {phase}"
            value = f"**Type:** {clean(event['category'], 30)}\n**UTC:** {start} — {end}"
            value += f"\n**{'Ends' if phase == 'In Progress' else 'Starts'}:** <t:{event['ends_at'] if phase == 'In Progress' else event['starts_at']}:R>"
            if event['notes']:
                value += f"\n{clean(event['notes'], 125)}"
            embed.add_field(name=name[:256], value=value[:1024], inline=False)
        if not items:
            embed.add_field(name='Schedule', value='No active or upcoming events.', inline=False)
        if total > len(items):
            embed.add_field(name='More events', value=f'{total-len(items)} more — see /event upcoming or /event calendar.', inline=False)
        embed.set_footer(text='SaS Event Updates | Shared schedule | Times in UTC')
        embed.timestamp = datetime.fromtimestamp(now, timezone.utc)
        return embed

    async def ensure_text_feed(self, guild_id, create=False):
        async with self.text_lock:
            settings = self.store.settings(guild_id)
            if settings is None:
                return None
            channel = self.text_channel()
            if channel is None:
                log.error('Event Updates channel %s is unavailable', self.text_channel_id)
                return None
            old_id = settings['text_message_id']
            old_channel_id = settings['text_message_channel_id']
            if old_id and old_channel_id == channel.id:
                try:
                    message = await channel.fetch_message(old_id)
                except discord.NotFound:
                    self.store.set_text_message(guild_id, None, None)
                else:
                    await message.edit(embed=self.text_payload(guild_id), allowed_mentions=discord.AllowedMentions.none())
                    self.store.set_text_message(guild_id, message.id, channel.id)
                    return message
            if create or old_id:
                message = await channel.send(embed=self.text_payload(guild_id), allowed_mentions=discord.AllowedMentions.none())
                self.store.set_text_message(guild_id, message.id, channel.id)
                return message
            return None

    async def refresh_text_safe(self, guild_id, create=False):
        try:
            return await self.ensure_text_feed(guild_id, create=create)
        except (discord.HTTPException, OSError, ValueError):
            log.exception('Unable to update shared text events in channel %s', self.text_channel_id)
            return None

    async def retire_legacy_cards(self, guild_id):
        for event in self.store.legacy_event_cards(guild_id):
            channel = self.bot.guild.get_channel(event['channel_id'])
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                message = await channel.fetch_message(event['message_id'])
            except discord.NotFound:
                self.store.mark_card_missing(event['id'])
                continue
            except discord.HTTPException:
                log.exception('Unable to inspect old event card #%s', event['id'])
                continue
            if message.author.id != self.bot.user.id:
                self.store.mark_card_missing(event['id'])
                continue
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.exception('Unable to delete old event card #%s', event['id'])
                continue
            self.store.mark_card_missing(event['id'])

    def board_channel(self):
        channel = self.bot.guild.get_channel(self.board_channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.id != self.board_channel_id:
            return None
        return channel

    async def ensure_board(self, guild_id, create=False):
        async with self.board_lock:
            settings = self.store.settings(guild_id)
            if settings is None:
                return None
            channel = self.board_channel()
            if channel is None:
                log.error('SaS Event Center channel %s is unavailable in this server', self.board_channel_id)
                return None
            previous_id = settings['calendar_message_id']
            previous_channel_id = settings['calendar_message_channel_id'] or settings['channel_id']
            same_channel = previous_channel_id == channel.id
            if previous_id and same_channel:
                try:
                    message = await channel.fetch_message(previous_id)
                except discord.NotFound:
                    self.store.set_board(guild_id, None)
                else:
                    embed, file = self.board_payload(guild_id)
                    edited = await message.edit(embed=embed, attachments=[file], allowed_mentions=discord.AllowedMentions.none())
                    result = edited or message
                    self.store.set_board(guild_id, result.id, channel_id=channel.id)
                    return result
            if create or previous_id:
                embed, file = self.board_payload(guild_id)
                message = await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
                self.store.set_board(guild_id, message.id, channel_id=channel.id)
                if previous_id and not same_channel:
                    old_channel = self.bot.guild.get_channel(previous_channel_id)
                    if isinstance(old_channel, discord.TextChannel):
                        try:
                            old_message = await old_channel.fetch_message(previous_id)
                            if old_message.author.id == self.bot.user.id:
                                await old_message.delete()
                        except (discord.HTTPException, AttributeError):
                            log.warning('Old SaS Event Center graphic could not be removed from channel %s', previous_channel_id)
                return message
            return None

    async def refresh_board(self, guild_id):
        return await self.ensure_board(guild_id)

    async def refresh_board_safe(self, guild_id, create=False):
        try:
            return await self.ensure_board(guild_id, create=create)
        except (discord.HTTPException, OSError, ValueError):
            log.exception('Could not synchronize shared SaS event calendar in server %s', guild_id)
            return None

    async def show_calendar(self, interaction, week_offset=0, update=False, view=None, publish=False):
        if interaction.guild_id != getattr(self.bot.guild, 'id', None):
            return await interaction.response.send_message('Only available in the SaS server.', ephemeral=True)
        if publish:
            settings = self.store.settings(interaction.guild_id)
            if settings is None:
                return await interaction.response.send_message('Run /event setup first.', ephemeral=True)
            channel = self.board_channel()
            if channel is None:
                return await interaction.response.send_message(f'Event Center channel {self.board_channel_id} is unavailable. Check the channel ID and bot permissions.', ephemeral=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                message = await self.ensure_board(interaction.guild_id, create=True)
                if not message:
                    return await interaction.followup.send('Calendar channel is unavailable.', ephemeral=True)
            except discord.HTTPException:
                log.exception('Unable to publish SaS weekly calendar')
                return await interaction.followup.send('Calendar could not be posted. Check channel permissions.', ephemeral=True)
            return await interaction.followup.send(f'Combined event calendar updated in {channel.mention}: {message.jump_url}', ephemeral=True)
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

        @group.command(name='setup', description='Configure legacy event settings and the shared event feeds')
        async def setup(interaction: discord.Interaction, channel: discord.TextChannel, notify_role: discord.Role = None):
            if not self.is_staff(interaction):
                return await interaction.response.send_message('Staff access required.', ephemeral=True)
            permissions = channel.permissions_for(interaction.guild.me)
            if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.attach_files, permissions.read_message_history)):
                return await interaction.response.send_message('The bot needs View Channel, Send Messages, Embed Links, Attach Files and Read Message History.', ephemeral=True)
            self.store.set_channel(interaction.guild_id, channel.id, notify_role.id if notify_role else None)
            await interaction.response.send_message(f"Event administration configured. The combined text list posts in <#{self.text_channel_id}> and the graphic posts in <#{self.board_channel_id}>. Individual channel reminders and role pings are disabled; private opt-in reminders remain available.", ephemeral=True)

        @group.command(name='create', description='Schedule an event using the in-game server time in UTC')
        @app_commands.describe(title='Event name', start_utc='Start YYYY-MM-DD HH:MM:SS UTC', category='Event color category', notes='Optional event details', end_utc='Optional end YYYY-MM-DD HH:MM:SS UTC')
        async def create(interaction: discord.Interaction, title: str, start_utc: str, category: Literal['Alliance','Events','Deals','Port Inspection','Other'] = 'Alliance', notes: str = '', end_utc: str = ''):
            if not self.is_staff(interaction):
                return await interaction.response.send_message('Staff access required.', ephemeral=True)
            settings = self.store.settings(interaction.guild_id)
            if settings is None:
                return await interaction.response.send_message('Run /event setup first.', ephemeral=True)
            try:
                starts_at = parse_utc(start_utc, future=False)
                ends_at = parse_utc(end_utc, future=False) if end_utc.strip() else starts_at + 3600
                validate_event_window(starts_at, ends_at, int(datetime.now(timezone.utc).timestamp()))
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
            feed = await self.refresh_text_safe(interaction.guild_id, create=True)
            board = await self.refresh_board_safe(interaction.guild_id, create=True)
            if feed and board:
                return await interaction.followup.send(
                    f"Event **{clean(title, 100)}** (#{event_id}) added to the shared updates: {feed.jump_url}\nGraphic: {board.jump_url}",
                    ephemeral=True
                )
            if not feed:
                return await interaction.followup.send(
                    f"Event #{event_id} was saved, but the text overview could not be posted in <#{self.text_channel_id}>. Check bot permissions and use /event updates.",
                    ephemeral=True
                )
            await interaction.followup.send(
                f"Event #{event_id} is in {feed.jump_url}, but the graphic could not be refreshed in <#{self.board_channel_id}>.",
                ephemeral=True
            )

        @group.command(name='calendar', description='Browse seven days or publish the combined Events overview')
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
                starts_at = parse_utc(start_utc, future=False) if start_utc.strip() else original['starts_at']
                ends_at = parse_utc(end_utc, future=False) if end_utc.strip() else (starts_at + original['ends_at'] - original['starts_at'] if start_utc.strip() else original['ends_at'])
                validate_event_window(starts_at, ends_at, int(datetime.now(timezone.utc).timestamp()))
                if len(new_title) < 3 or len(new_title) > 100 or len(new_notes) > 900 or new_category not in CATEGORIES:
                    raise ValueError('Invalid title, category or notes.')
                if not self.store.edit(event_id, interaction.guild_id, new_title, new_category, new_notes, starts_at, ends_at):
                    raise ValueError('Event no longer available.')
            except ValueError as error:
                return await interaction.response.send_message(str(error), ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            feed = await self.refresh_text_safe(interaction.guild_id, create=True)
            await self.refresh_board_safe(interaction.guild_id, create=True)
            await interaction.followup.send(
                f"Event #{event_id} updated in the shared list." + (f" {feed.jump_url}" if feed else " The text channel is currently unavailable."),
                ephemeral=True
            )

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
            await self.refresh_text_safe(interaction.guild_id, create=True)
            await self.refresh_board_safe(interaction.guild_id, create=True)
            await interaction.response.send_message(
                f'Event #{event_id} cancelled and removed from the shared active schedule.',
                ephemeral=True
            )

        @group.command(name='updates', description='Publish or refresh the shared event text list')
        async def updates(interaction: discord.Interaction):
            if not self.is_staff(interaction):
                return await interaction.response.send_message('Staff access required.', ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            message = await self.refresh_text_safe(interaction.guild_id, create=True)
            if not message:
                return await interaction.followup.send(
                    f'Event text channel <#{self.text_channel_id}> is unavailable. Check View Channel, Send Messages and Embed Links.',
                    ephemeral=True
                )
            await interaction.followup.send(f'Shared event list updated: {message.jump_url}', ephemeral=True)

        @group.command(name='remind', description='Subscribe to private reminders for one event')
        async def remind(interaction: discord.Interaction, event_id: int):
            if interaction.guild_id != getattr(self.bot.guild, 'id', None):
                return await interaction.response.send_message('Only available in the SaS server.', ephemeral=True)
            event = self.store.event(event_id)
            if not event or event['guild_id'] != interaction.guild_id:
                return await interaction.response.send_message('Event not found.', ephemeral=True)
            if not self.store.subscribe(event_id, interaction.user.id):
                return await interaction.response.send_message('This event is no longer accepting reminders.', ephemeral=True)
            await interaction.response.send_message(
                f'Private reminders enabled for event #{event_id}. You will receive DMs 1 hour and 15 minutes before start if subscribed before those deadlines.',
                ephemeral=True
            )

        @group.command(name='unremind', description='Disable your private reminders for one event')
        async def unremind(interaction: discord.Interaction, event_id: int):
            if interaction.guild_id != getattr(self.bot.guild, 'id', None):
                return await interaction.response.send_message('Only available in the SaS server.', ephemeral=True)
            event = self.store.event(event_id)
            if not event or event['guild_id'] != interaction.guild_id:
                return await interaction.response.send_message('Event not found.', ephemeral=True)
            self.store.unsubscribe(event_id, interaction.user.id)
            await interaction.response.send_message(f'Private reminders disabled for event #{event_id}.', ephemeral=True)

        self.bot.tree.add_command(group, guild=self.bot.guild)
        self.group_registered = True

    @tasks.loop(seconds=30)
    async def scheduler(self):
        if not self.bot.configured:
            return
        now = int(datetime.now(timezone.utc).timestamp())
        notifications = []
        for reminder in self.store.due(now):
            event = self.store.event(reminder['event_id'])
            stage = reminder['stage']
            if (event['state'] != 'active' or now >= event['ends_at']
                    or now - reminder['due_at'] > 600
                    or (stage != 'start' and now >= event['starts_at'])):
                self.store.reminder_state(event['id'], stage, 'skipped')
                continue
            notifications.append(reminder)
        if notifications:
            feed = await self.refresh_text_safe(self.bot.guild.id, create=True, alerts=notifications)
            if feed:
                for reminder in notifications:
                    self.store.reminder_state(reminder['event_id'], reminder['stage'], 'sent', feed.id)
        settings = self.store.settings(self.bot.guild.id)
        if settings and not settings['text_message_id'] and self.store.upcoming(self.bot.guild.id, now):
            await self.refresh_text_safe(self.bot.guild.id, create=True)
        for item in self.store.text_feeds_to_refresh(now, self.text_channel_id):
            await self.refresh_text_safe(item['guild_id'], create=True)
        settings = self.store.settings(self.bot.guild.id)
        if settings and settings['text_message_id']:
            await self.retire_legacy_cards(self.bot.guild.id)
        if settings and not settings['calendar_message_id'] and self.store.upcoming(self.bot.guild.id, now):
            await self.refresh_board_safe(self.bot.guild.id, create=True)
        for item in self.store.boards_to_refresh(now, self.board_channel_id):
            await self.refresh_board_safe(item['guild_id'])
        for job in self.store.due_dm():
            event = self.store.event(job['event_id'])
            if event['state'] != 'active':
                self.store.dm_state(job['event_id'], job['stage'], job['user_id'], 'skipped')
                continue
            if int(datetime.now(timezone.utc).timestamp()) >= event['starts_at']:
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
