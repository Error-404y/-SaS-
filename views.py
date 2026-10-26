import logging
import unicodedata

import discord

log = logging.getLogger('verification')


def embed(title, description=''):
    result = discord.Embed(title=title[:256], description=description[:4096], color=0x243B53)
    result.set_author(name='SaS / POLICE SIMULATOR')
    result.set_footer(text='Verification service | Europe/Zurich')
    return result


async def send_embed(interaction, message, view=None):
    kwargs = dict(embed=message, ephemeral=True)
    if view is not None:
        kwargs['view'] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
        return
    try:
        await interaction.response.send_message(**kwargs)
    except discord.HTTPException as error:
        if error.code != 40060:
            raise
        await interaction.followup.send(**kwargs)


async def reply(interaction, title, description=''):
    await send_embed(interaction, embed(title, description))


async def report_error(interaction, error):
    log.error('Interaction failed', exc_info=(type(error), error, error.__traceback__))
    try:
        await reply(interaction, 'Unable to complete action', 'Please try again or contact staff.')
    except discord.HTTPException:
        log.exception('Could not send error response')


def valid_username(value):
    return (1 <= len(value) <= 100 and any(c.isalnum() for c in value)
            and not any(unicodedata.category(c).startswith('C') for c in value)
            and not any(s in value.casefold() for s in ('https:', 'http:', '<@', '@everyone', '@here')))


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await report_error(interaction, error)


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        await report_error(interaction, error)


class ApplicationModal(SafeModal, title='Verify your account'):
    username = discord.ui.TextInput(label='Police Simulator username', max_length=100)

    def __init__(self, bot, kind='member'):
        super().__init__(timeout=300, title=f'Verify - {kind.title()}')
        self.bot = bot
        self.kind = kind
        self.clan = None
        if kind == 'allies':
            self.clan = discord.ui.TextInput(label='Clan name', placeholder='For example: SaS', max_length=12)
            self.add_item(self.clan)

    async def on_submit(self, interaction):
        await self.bot.submit(interaction, str(self.username).strip(), self.kind, str(self.clan).strip() if self.clan is not None else '')


class RejectModal(SafeModal, title='Reject application'):
    reason = discord.ui.TextInput(label='Reason', style=discord.TextStyle.paragraph, max_length=800)

    def __init__(self, bot, application_id):
        super().__init__(timeout=300)
        self.bot, self.application_id = bot, application_id

    async def on_submit(self, interaction):
        await self.bot.decide(interaction, self.application_id, 'rejected', str(self.reason).strip())


class VerifyView(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.verify.disabled = self.allies.disabled = bot.db.paused()['active']

    @discord.ui.button(label='Verify - Member', style=discord.ButtonStyle.primary, custom_id='sas:verify:v1', row=0)
    async def verify(self, interaction, button):
        await self.open_application(interaction, 'member')

    @discord.ui.button(label='Verify - Allies', style=discord.ButtonStyle.secondary, custom_id='sas:verify:allies:v1', row=0)
    async def allies(self, interaction, button):
        await self.open_application(interaction, 'allies')

    async def open_application(self, interaction, kind):
        if not self.bot.public_context(interaction):
            return await reply(interaction, 'Not available', 'Use the verification channel.')
        if self.bot.db.paused()['active']:
            return await reply(interaction, 'Applications paused', self.bot.db.paused()['reason'])
        if self.bot.db.is_verified(interaction.guild_id, interaction.user.id):
            return await reply(interaction, 'Already accepted', 'Your verification is accepted. Any remaining role update will finish automatically.')
        if any(r.id in self.bot.verified_role_ids() for r in getattr(interaction.user, 'roles', [])):
            return await reply(interaction, 'Already verified', 'You already have Member or Allies access.')
        if self.bot.db.pending(interaction.guild_id, interaction.user.id):
            return await reply(interaction, 'Application pending', 'Your request is already waiting for staff review.')
        until = self.bot.db.cooldown_until(interaction.guild_id, interaction.user.id)
        if until:
            return await reply(interaction, 'Please wait before reapplying', f'You can submit another request <t:{int(until.timestamp())}:R>.')
        await interaction.response.send_modal(ApplicationModal(self.bot, kind))

    @discord.ui.button(label='Name change', style=discord.ButtonStyle.secondary, custom_id='sas:profile:name:v1', row=1)
    async def name_change(self, interaction, button):
        if not self.bot.public_context(interaction):
            return await reply(interaction, 'Not available', 'Use the verification channel.')
        await interaction.response.send_modal(NameChangeModal(self.bot))

    @discord.ui.button(label='Clan change', style=discord.ButtonStyle.secondary, custom_id='sas:profile:clan:v1', row=1)
    async def clan_change(self, interaction, button):
        if not self.bot.public_context(interaction):
            return await reply(interaction, 'Not available', 'Use the verification channel.')
        await send_embed(interaction, embed('Are you trying to join SaS?',
            '**Yes** sends a Member verification request to staff.\n**No** lets you request another clan and Allies access.\nYour current nickname and access stay in place until approval.'), ClanChoiceView(self.bot))



class NameChangeModal(SafeModal, title='Request a name change'):
    username = discord.ui.TextInput(label='New in-game username', placeholder='Username only, without a clan tag', max_length=100)

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction):
        await self.bot.submit_change(interaction, 'name', str(self.username).strip())


class ClanChangeModal(SafeModal, title='Request a clan change'):
    clan = discord.ui.TextInput(label='What clan are you joining?', max_length=12)

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction):
        await self.bot.submit_change(interaction, 'clan', str(self.clan).strip())


class ClanChoiceView(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    @discord.ui.button(label='Yes - Join SaS', style=discord.ButtonStyle.primary)
    async def join_sas(self, interaction, button):
        await self.bot.submit_change(interaction, 'join_member')

    @discord.ui.button(label='No - Another clan', style=discord.ButtonStyle.secondary)
    async def other_clan(self, interaction, button):
        await interaction.response.send_modal(ClanChangeModal(self.bot))


class PauseModal(SafeModal, title='Pause verification applications'):
    reason = discord.ui.TextInput(label='Explanation shown to applicants', style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction):
        await self.bot.set_application_pause(interaction, True, str(self.reason).strip())


class PauseControls(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Pause applications', style=discord.ButtonStyle.secondary, custom_id='sas:control:pause:v1')
    async def pause(self, interaction, button):
        if not self.bot.control_context(interaction) or not any(role.id == self.bot.ids['STAFF_ROLE_ID'] for role in getattr(interaction.user, 'roles', [])):
            return await reply(interaction, 'High-Rank role required', 'Use the staff control center with the High-Rank role.')
        await interaction.response.send_modal(PauseModal(self.bot))

    @discord.ui.button(label='Resume applications', style=discord.ButtonStyle.primary, custom_id='sas:control:resume:v1')
    async def resume(self, interaction, button):
        await self.bot.set_application_pause(interaction, False)


class ReviewView(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def application(self, interaction):
        if not self.bot.staff_context(interaction):
            await reply(interaction, 'Not allowed', 'Only administrators or High-Rank staff can review requests here.')
            return None
        row = self.bot.db.by_message(interaction.message.id)
        if not row or row['status'] != 'pending' or row['review_deleted']:
            await reply(interaction, 'Already handled', 'This application is no longer awaiting review.')
            return None
        return row

    @discord.ui.button(label='Accept', style=discord.ButtonStyle.success, custom_id='sas:accept:v1')
    async def accept(self, interaction, button):
        row = await self.application(interaction)
        if row:
            await self.bot.decide(interaction, row['id'], 'accepted')

    @discord.ui.button(label='Reject', style=discord.ButtonStyle.danger, custom_id='sas:reject:v1')
    async def reject(self, interaction, button):
        row = await self.application(interaction)
        if row:
            await interaction.response.send_modal(RejectModal(self.bot, row['id']))
