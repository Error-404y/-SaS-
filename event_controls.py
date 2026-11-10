from datetime import datetime, timezone

import discord

from event_center import CATEGORIES, clean, event_phase, parse_utc, validate_event_window


def timestamp_utc(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def selected_event(manager, event_id):
    try:
        identifier = int(event_id)
    except (TypeError, ValueError):
        return None
    event = manager.store.event(identifier)
    if (
        event is None or event['guild_id'] != manager.target_guild_id
        or event['state'] != 'active'
        or event['ends_at'] <= int(datetime.now(timezone.utc).timestamp())
    ):
        return None
    return event


def event_details(event):
    result = discord.Embed(
        title=f"SaS | Event #{event['id']}",
        description=f"**{clean(event['title'], 100)}**",
        color=0x318BCE
    )
    result.add_field(name='CATEGORY', value=clean(event['category'], 60), inline=True)
    result.add_field(name='STATUS', value=event_phase(event), inline=True)
    result.add_field(name='START | UTC', value=timestamp_utc(event['starts_at']), inline=False)
    result.add_field(name='END | UTC', value=timestamp_utc(event['ends_at']), inline=False)
    result.add_field(name='DETAILS', value=clean(event['notes'], 900) or 'No additional details.', inline=False)
    return result


async def permission_denied(interaction):
    await interaction.response.send_message(
        'Event administration is limited to authorized officers in the designated command channel.',
        ephemeral=True
    )


async def synchronized(manager, interaction, message):
    feed = await manager.refresh_text_safe(manager.target_guild_id, create=True)
    graphic = await manager.refresh_board_safe(manager.target_guild_id, create=True)
    await manager.refresh_management_panel_safe(create=True)
    text_status = f'[Event Updates]({feed.jump_url})' if feed else 'Event Updates unavailable'
    graph_status = f'[Graphical Overview]({graphic.jump_url})' if graphic else 'Graphical overview unavailable'
    await interaction.followup.send(
        f'{message}\n\nSaS synchronization: {text_status} | {graph_status}',
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none()
    )


class CreateEventModal(discord.ui.Modal, title='SaS | Create Event'):
    event_title = discord.ui.TextInput(
        label='Event Name', min_length=3, max_length=100,
        placeholder='Oilfield Strike'
    )
    start_time = discord.ui.TextInput(
        label='Start | YYYY-MM-DD HH:MM:SS UTC',
        max_length=19, placeholder='2026-12-01 19:00:00'
    )
    end_time = discord.ui.TextInput(
        label='End | YYYY-MM-DD HH:MM:SS UTC',
        max_length=19, required=False, placeholder='Optional; default is start + 1 hour'
    )
    category = discord.ui.TextInput(
        label='Category', max_length=40, default='Alliance',
        placeholder='Alliance / Events / Deals / Port Inspection / Other'
    )
    details = discord.ui.TextInput(
        label='Event Details', style=discord.TextStyle.paragraph,
        max_length=900, required=False
    )

    def __init__(self, manager):
        super().__init__(timeout=300)
        self.manager = manager

    async def on_submit(self, interaction):
        manager = self.manager
        if not manager.is_staff(interaction):
            return await permission_denied(interaction)
        title = str(self.event_title.value).strip()
        category = str(self.category.value).strip()
        details = str(self.details.value).strip()
        now = int(datetime.now(timezone.utc).timestamp())
        try:
            starts = parse_utc(str(self.start_time.value), now=now)
            ends = parse_utc(str(self.end_time.value), now=now, future=False) if str(self.end_time.value).strip() else starts + 3600
            validate_event_window(starts, ends, now)
            if category not in CATEGORIES:
                raise ValueError('Category must be Alliance, Events, Deals, Port Inspection or Other.')
            channel = manager.text_channel()
            if channel is None:
                raise ValueError('SaS Event Updates channel is unavailable. Check the bot permissions.')
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        event_id = manager.store.create(
            manager.target_guild_id, channel.id, title, category,
            details, starts, interaction.user.id, now=now, ends_at=ends
        )
        await synchronized(manager, interaction, f'Event **#{event_id} | {clean(title, 100)}** created.')


class EditEventModal(discord.ui.Modal, title='SaS | Edit Event'):
    def __init__(self, manager, event):
        super().__init__(timeout=300)
        self.manager = manager
        self.event_id = event['id']
        self.event_title = discord.ui.TextInput(
            label='Event Name', default=event['title'],
            min_length=3, max_length=100
        )
        self.start_time = discord.ui.TextInput(
            label='Start | YYYY-MM-DD HH:MM:SS UTC',
            default=timestamp_utc(event['starts_at']), max_length=19
        )
        self.end_time = discord.ui.TextInput(
            label='End | YYYY-MM-DD HH:MM:SS UTC',
            default=timestamp_utc(event['ends_at']), max_length=19
        )
        self.category = discord.ui.TextInput(
            label='Category', default=event['category'], max_length=40
        )
        self.details = discord.ui.TextInput(
            label='Event Details', default=event['notes'],
            style=discord.TextStyle.paragraph, required=False, max_length=900
        )
        for field in (self.event_title, self.start_time, self.end_time, self.category, self.details):
            self.add_item(field)

    async def on_submit(self, interaction):
        manager = self.manager
        if not manager.is_staff(interaction):
            return await permission_denied(interaction)
        event = selected_event(manager, self.event_id)
        if event is None:
            return await interaction.response.send_message('This event is no longer active.', ephemeral=True)
        now = int(datetime.now(timezone.utc).timestamp())
        try:
            starts = parse_utc(str(self.start_time.value), now=now, future=False)
            ends = parse_utc(str(self.end_time.value), now=now, future=False)
            validate_event_window(starts, ends, now)
            title = str(self.event_title.value).strip()
            category = str(self.category.value).strip()
            details = str(self.details.value).strip()
            if len(title) < 3 or category not in CATEGORIES:
                raise ValueError('Title must have 3–100 characters; choose a valid event category.')
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = manager.store.edit(
            self.event_id, manager.target_guild_id, title,
            category, details, starts, ends, now=now
        )
        if not updated:
            return await interaction.followup.send('This event is no longer active.', ephemeral=True)
        await synchronized(manager, interaction, f'Event **#{self.event_id} | {clean(title, 100)}** updated.')


class PostponeEventModal(discord.ui.Modal, title='SaS | Postpone Event'):
    def __init__(self, manager, event):
        super().__init__(timeout=300)
        self.manager = manager
        self.event_id = event['id']
        self.new_start = discord.ui.TextInput(
            label='New Start | YYYY-MM-DD HH:MM:SS UTC', max_length=19,
            default=timestamp_utc(event['starts_at'])
        )
        self.new_end = discord.ui.TextInput(
            label='New End | UTC (optional)', required=False,
            max_length=19, placeholder='Leave blank to preserve original duration'
        )
        self.add_item(self.new_start)
        self.add_item(self.new_end)

    async def on_submit(self, interaction):
        manager = self.manager
        if not manager.is_staff(interaction):
            return await permission_denied(interaction)
        event = selected_event(manager, self.event_id)
        if event is None:
            return await interaction.response.send_message('This event is no longer active.', ephemeral=True)
        now = int(datetime.now(timezone.utc).timestamp())
        try:
            starts = parse_utc(str(self.new_start.value), now=now)
            ends = (
                parse_utc(str(self.new_end.value), now=now, future=False)
                if str(self.new_end.value).strip()
                else starts + event['ends_at'] - event['starts_at']
            )
            validate_event_window(starts, ends, now)
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = manager.store.edit(
            self.event_id, manager.target_guild_id, event['title'],
            event['category'], event['notes'], starts, ends, now=now
        )
        if not updated:
            return await interaction.followup.send('This event is no longer active.', ephemeral=True)
        await synchronized(
            manager, interaction,
            f"Event **#{self.event_id} | {clean(event['title'], 100)}** postponed to **{timestamp_utc(starts)} UTC**."
        )


class CancelConfirmView(discord.ui.View):
    def __init__(self, manager, event_id, user_id):
        super().__init__(timeout=180)
        self.manager = manager
        self.event_id = event_id
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or not self.manager.is_staff(interaction):
            await permission_denied(interaction)
            return False
        return True

    @discord.ui.button(label='Confirm Cancellation', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.manager.store.cancel(self.event_id, self.manager.target_guild_id):
            return await interaction.followup.send('Event is no longer active.', ephemeral=True)
        await synchronized(self.manager, interaction, f'Event **#{self.event_id}** cancelled.')
        self.stop()
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label='Keep Event', style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content='Cancellation dismissed.', embed=None, view=None)


class EventContinueView(discord.ui.View):
    def __init__(self, manager, event_id, mode, user_id):
        super().__init__(timeout=180)
        self.manager = manager
        self.event_id = event_id
        self.mode = mode
        self.user_id = user_id
        self.add_item(ContinueButton())

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or not self.manager.is_staff(interaction):
            await permission_denied(interaction)
            return False
        return True


class ContinueButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label='Continue', style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        panel = self.view
        await open_event_action(panel.manager, interaction, panel.event_id, panel.mode)


class EventIdModal(discord.ui.Modal, title='SaS | Find Event by ID'):
    identifier = discord.ui.TextInput(
        label='Event ID', placeholder='e.g. 12', max_length=12,
        min_length=1
    )

    def __init__(self, manager, mode):
        super().__init__(timeout=180)
        self.manager = manager
        self.mode = mode

    async def on_submit(self, interaction):
        if not self.manager.is_staff(interaction):
            return await permission_denied(interaction)
        event = selected_event(self.manager, str(self.identifier.value).strip())
        if event is None:
            return await interaction.response.send_message('Active event not found in the SaS server.', ephemeral=True)
        if self.mode == 'inspect':
            return await interaction.response.send_message(embed=event_details(event), ephemeral=True)
        if self.mode == 'cancel':
            return await interaction.response.send_message(
                content='Confirm cancellation of this event?',
                embed=event_details(event),
                view=CancelConfirmView(self.manager, event['id'], interaction.user.id),
                ephemeral=True
            )
        await interaction.response.send_message(
            content='Event located. Select Continue to open the editing form.',
            embed=event_details(event),
            view=EventContinueView(self.manager, event['id'], self.mode, interaction.user.id),
            ephemeral=True
        )


class EventPicker(discord.ui.Select):
    def __init__(self, manager, mode, events):
        options = []
        for event in events:
            options.append(discord.SelectOption(
                label=f"#{event['id']} | {event['title']}"[:100],
                value=str(event['id']),
                description=f"{event['category']} | {timestamp_utc(event['starts_at'])} UTC"[:100],
            ))
        super().__init__(
            placeholder='Choose an event to manage',
            min_values=1,
            max_values=1,
            options=options
        )
        self.manager = manager
        self.mode = mode

    async def callback(self, interaction):
        await open_event_action(self.manager, interaction, int(self.values[0]), self.mode)


class EventPickerView(discord.ui.View):
    def __init__(self, manager, mode, events, user_id):
        super().__init__(timeout=180)
        self.manager = manager
        self.user_id = user_id
        self.mode = mode
        self.add_item(EventPicker(manager, mode, events))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id or not self.manager.is_staff(interaction):
            await permission_denied(interaction)
            return False
        return True

    @discord.ui.button(label='Enter Event ID', style=discord.ButtonStyle.secondary)
    async def by_id(self, interaction, button):
        await interaction.response.send_modal(EventIdModal(self.manager, self.mode))


async def open_event_action(manager, interaction, event_id, mode):
    if not manager.is_staff(interaction):
        return await permission_denied(interaction)
    event = selected_event(manager, event_id)
    if event is None:
        return await interaction.response.send_message('Active event not found in the SaS server.', ephemeral=True)
    if mode == 'edit':
        return await interaction.response.send_modal(EditEventModal(manager, event))
    if mode == 'postpone':
        return await interaction.response.send_modal(PostponeEventModal(manager, event))
    if mode == 'cancel':
        return await interaction.response.send_message(
            content='This action will remove the event from the active SaS schedule and cancel pending reminders.',
            embed=event_details(event),
            view=CancelConfirmView(manager, event['id'], interaction.user.id),
            ephemeral=True
        )
    await interaction.response.send_message(embed=event_details(event), ephemeral=True)


class EventControlView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=None)
        self.manager = manager

    async def interaction_check(self, interaction):
        if not self.manager.is_staff(interaction):
            await permission_denied(interaction)
            return False
        return True

    async def choose(self, interaction, mode):
        events = self.manager.store.manage_events(self.manager.target_guild_id)
        if not events:
            return await interaction.response.send_message('No active or upcoming SaS events are available.', ephemeral=True)
        await interaction.response.send_message(
            content=f'Select an event to {mode}. The list shows the next 25 events; use Enter Event ID for others.',
            view=EventPickerView(self.manager, mode, events, interaction.user.id),
            ephemeral=True
        )

    @discord.ui.button(label='Create', custom_id='sas:events:control:create', style=discord.ButtonStyle.success, row=0)
    async def create(self, interaction, button):
        await interaction.response.send_modal(CreateEventModal(self.manager))

    @discord.ui.button(label='Edit', custom_id='sas:events:control:edit', style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction, button):
        await self.choose(interaction, 'edit')

    @discord.ui.button(label='Postpone', custom_id='sas:events:control:postpone', style=discord.ButtonStyle.secondary, row=0)
    async def postpone(self, interaction, button):
        await self.choose(interaction, 'postpone')

    @discord.ui.button(label='Cancel', custom_id='sas:events:control:cancel', style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction, button):
        await self.choose(interaction, 'cancel')

    @discord.ui.button(label='Inspect', custom_id='sas:events:control:inspect', style=discord.ButtonStyle.secondary, row=0)
    async def inspect(self, interaction, button):
        await self.choose(interaction, 'inspect')

    @discord.ui.button(label='Upcoming', custom_id='sas:events:control:upcoming', style=discord.ButtonStyle.secondary, row=1)
    async def upcoming(self, interaction, button):
        rows = self.manager.store.upcoming(self.manager.target_guild_id)
        await interaction.response.send_message(
            embed=self.manager.upcoming_embed(rows),
            ephemeral=True
        )

    @discord.ui.button(label='Calendar', custom_id='sas:events:control:calendar', style=discord.ButtonStyle.secondary, row=1)
    async def calendar(self, interaction, button):
        await self.manager.show_calendar(interaction)

    @discord.ui.button(label='Refresh Overviews', custom_id='sas:events:control:refresh', style=discord.ButtonStyle.primary, row=1)
    async def refresh(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await synchronized(self.manager, interaction, 'SaS event overviews refreshed.')
