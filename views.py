import logging
import unicodedata

import discord

log = logging.getLogger('verification')


def embed(title, description=''):
    return discord.Embed(title=title[:256], description=description[:4096], color=discord.Color.blurple())


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

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction):
        await self.bot.submit(interaction, str(self.username).strip())


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

    @discord.ui.button(label='Verify', style=discord.ButtonStyle.success, custom_id='sas:verify:v1')
    async def verify(self, interaction, button):
        if not self.bot.public_context(interaction):
            return await reply(interaction, 'Not available', 'Use the verification channel.')
        if self.bot.db.is_verified(interaction.guild_id, interaction.user.id):
            return await reply(interaction, 'Already accepted', 'Your verification is accepted. Any remaining role update will finish automatically.')
        if any(r.id == self.bot.ids['MEMBER_ROLE_ID'] for r in getattr(interaction.user, 'roles', [])):
            return await reply(interaction, 'Already verified', 'You already have the Member role.')
        if self.bot.db.pending(interaction.guild_id, interaction.user.id):
            return await reply(interaction, 'Application pending', 'Your request is already waiting for staff review.')
        await interaction.response.send_modal(ApplicationModal(self.bot))


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
