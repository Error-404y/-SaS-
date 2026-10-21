"""Police Simulator verification bot. All user-facing messages use embeds."""
import asyncio
import logging
import os

import discord
from discord.ext import tasks
from dotenv import load_dotenv

from database import Database

load_dotenv()
log = logging.getLogger('verification')
DEFAULTS = {
    'VERIFY_CATEGORY_ID': 1562540102394060840,
    'VERIFY_CHANNEL_ID': 1562540152276910093,
    'STAFF_ROLE_ID': 1562535759276417065,
    'PROGRESS_ROLE_ID': 1562535216050864209,
    'MEMBER_ROLE_ID': 1562535352290250842,
    'STAFF_CATEGORY_ID': 1562539595474927637,
    'REVIEW_CHANNEL_ID': 1562539651229683732,
}


def embed(title, description, color=discord.Color.blurple()):
    return discord.Embed(title=title, description=description, color=color)


async def reply(interaction, title, description):
    kwargs = dict(embed=embed(title, description), ephemeral=True)
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        log.error('Button failed', exc_info=(type(error), error, error.__traceback__))
        await reply(interaction, 'Unable to complete action', 'Please try again. Staff can check the bot logs.')


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        log.error('Modal failed', exc_info=(type(error), error, error.__traceback__))
        await reply(interaction, 'Unable to complete action', 'Please try again. Staff can check the bot logs.')


class ApplicationModal(SafeModal, title='Police Simulator verification'):
    username = discord.ui.TextInput(label='In-game username', placeholder='Your Police Simulator username',
                                    min_length=1, max_length=100)

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction):
        if not await self.bot.applicant_allowed(interaction):
            return
        name = str(self.username).strip()
        if not name or any(ord(c) < 32 for c in name):
            await reply(interaction, 'Invalid username', 'Enter a non-empty username without control characters.')
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.lock:
            row = self.bot.db.create(interaction.guild_id, interaction.user.id, name)
            if row is None:
                await reply(interaction, 'Already submitted', 'You already have an application awaiting review.')
                return
            await self.bot.try_process(row['id'])
            row = self.bot.db.get(row['id'])
        detail = 'Your request has been successfully submitted. Please wait until staff verify it.'
        if row['message_id'] is None:
            detail += '\nYour request is saved; delivery to staff will be retried automatically.'
        if row['submission_dm'] == 'blocked':
            detail += '\nI could not send you a DM. Enable direct messages from this server for future updates.'
        await reply(interaction, 'Application submitted', detail)


class VerifyView(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Verify', style=discord.ButtonStyle.success, custom_id='sas:verify:v1')
    async def verify(self, interaction, button):
        if await self.bot.applicant_allowed(interaction):
            await interaction.response.send_modal(ApplicationModal(self.bot))


class RejectModal(SafeModal, title='Reject verification request'):
    reason = discord.ui.TextInput(label='Why are you rejecting this request?', style=discord.TextStyle.paragraph,
                                  placeholder='Explain what the applicant should correct.', min_length=1, max_length=1000)

    def __init__(self, bot, application_id):
        super().__init__(timeout=300)
        self.bot, self.application_id = bot, application_id

    async def on_submit(self, interaction):
        reason = str(self.reason).strip()
        if not reason:
            await reply(interaction, 'Reason required', 'Please explain why this request is being rejected.')
            return
        await self.bot.decide(interaction, self.application_id, 'rejected', reason)


class ReviewView(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def application(self, interaction):
        if not await self.bot.staff_allowed(interaction):
            return None
        row = self.bot.db.by_message(interaction.message.id)
        if row is None or row['status'] != 'pending':
            await reply(interaction, 'Already handled', 'This request is no longer awaiting a decision.')
            return None
        if row['user_id'] == interaction.user.id:
            await reply(interaction, 'Another reviewer required', 'You cannot review your own application.')
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


class VerificationBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.ids = {key: int(os.getenv(key, value)) for key, value in DEFAULTS.items()}
        self.db = Database(os.getenv('DATABASE_PATH', 'data/verification.json'))
        self.lock = asyncio.Lock()
        self.guild = None
        self.configured = False

    async def setup_hook(self):
        self.add_view(VerifyView(self))
        self.add_view(ReviewView(self))

    async def validate(self):
        self.verify_channel = await self.fetch_channel(self.ids['VERIFY_CHANNEL_ID'])
        self.review_channel = await self.fetch_channel(self.ids['REVIEW_CHANNEL_ID'])
        if not all(isinstance(c, discord.TextChannel) for c in (self.verify_channel, self.review_channel)):
            raise RuntimeError('Verify and review channels must be server text channels.')
        self.guild = self.verify_channel.guild
        if self.review_channel.guild.id != self.guild.id:
            raise RuntimeError('Both channels must belong to the same server.')
        for channel, category_key in ((self.verify_channel, 'VERIFY_CATEGORY_ID'), (self.review_channel, 'STAFF_CATEGORY_ID')):
            if channel.category_id != self.ids[category_key]:
                raise RuntimeError(f'{channel.name} is not in the configured category.')
            permissions = channel.permissions_for(self.guild.me)
            if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.read_message_history)):
                raise RuntimeError(f'Missing View Channel, Send Messages, Embed Links or Read Message History in {channel.name}.')
        roles = {role.id: role for role in await self.guild.fetch_roles()}
        for key in ('STAFF_ROLE_ID', 'PROGRESS_ROLE_ID', 'MEMBER_ROLE_ID'):
            if self.ids[key] not in roles:
                raise RuntimeError(f'Role {key} does not exist.')
        if not self.guild.me.guild_permissions.manage_roles:
            raise RuntimeError('The bot needs Manage Roles permission.')
        for key in ('PROGRESS_ROLE_ID', 'MEMBER_ROLE_ID'):
            role = roles[self.ids[key]]
            if role.managed or role.is_default() or role >= self.guild.me.top_role:
                raise RuntimeError(f'Move the bot role above {role.name}; target roles must be ordinary roles.')

    async def ensure_panel(self):
        panel_id = self.db.setting('panel_message')
        panel = embed('Police Simulator verification',
                      'Welcome! Click **Verify** and enter your in-game username.\n'
                      'High-Rank staff will review your application. You will receive a DM with the result.')
        if panel_id:
            try:
                message = await self.verify_channel.fetch_message(panel_id)
                await message.edit(embed=panel, view=VerifyView(self))
                return
            except discord.NotFound:
                pass
        message = await self.verify_channel.send(embed=panel, view=VerifyView(self))
        self.db.set_setting('panel_message', message.id)

    async def on_ready(self):
        async with self.lock:
            if self.configured:
                return
            try:
                await self.validate()
                await self.ensure_panel()
                self.configured = True
                self.recover.start()
                log.info('Ready as %s in %s', self.user, self.guild.name)
            except Exception:
                log.exception('Startup configuration failed; fix permissions/settings and restart.')
                await self.close()

    async def applicant_allowed(self, interaction):
        if not self.configured or interaction.guild_id != self.guild.id or interaction.channel_id != self.ids['VERIFY_CHANNEL_ID']:
            await reply(interaction, 'Verification unavailable', 'Use the Verify button in the configured verification channel.')
            return False
        member = await self.guild.fetch_member(interaction.user.id)
        if any(role.id == self.ids['MEMBER_ROLE_ID'] for role in member.roles):
            await reply(interaction, 'Already verified', 'You already have the Member role.')
            return False
        return True

    async def staff_allowed(self, interaction):
        if not self.configured or interaction.guild_id != self.guild.id or interaction.channel_id != self.ids['REVIEW_CHANNEL_ID']:
            await reply(interaction, 'Not allowed', 'Review applications in the configured staff channel.')
            return False
        member = await self.guild.fetch_member(interaction.user.id)
        if not any(role.id == self.ids['STAFF_ROLE_ID'] for role in member.roles):
            await reply(interaction, 'Not allowed', 'Only the configured High-Rank role can review applications.')
            return False
        return True

    async def decide(self, interaction, application_id, status, reason=None):
        if not await self.staff_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            row = self.db.get(application_id)
            if not row or row['guild_id'] != interaction.guild_id or row['user_id'] == interaction.user.id:
                await reply(interaction, 'Not allowed', 'This application cannot be reviewed by you.')
                return
            if not self.db.decide(application_id, interaction.user.id, status, reason):
                await reply(interaction, 'Already handled', 'Another staff member has already reviewed this application.')
                return
            await self.try_process(application_id)
            row = self.db.get(application_id)
        detail = f'Application #{application_id} was {status}.'
        if not row['roles_done']:
            detail += '\nRole updates are pending and will be retried. Check permissions and whether the applicant is still in the server.'
        if row['decision_dm'] == 'blocked':
            detail += '\nThe applicant has DMs disabled; the decision is still saved.'
        await reply(interaction, 'Decision recorded', detail)

    def review_embed(self, row):
        colors = {'pending': discord.Color.gold(), 'accepted': discord.Color.green(), 'rejected': discord.Color.red()}
        result = embed(f"Verification #{row['id']} — {row['status'].title()}",
                       f"Applicant: <@{row['user_id']}>\nDiscord ID: {row['user_id']}", colors[row['status']])
        result.add_field(name='Police Simulator username', value=discord.utils.escape_markdown(row['username']), inline=False)
        result.add_field(name='Submitted (UTC)', value=row['created_at'])
        if row['reviewer_id']:
            result.add_field(name='Reviewed by', value=f"<@{row['reviewer_id']}>")
            result.add_field(name='Decision (UTC)', value=row['decided_at'])
        if row['reason']:
            result.add_field(name='Reason', value=discord.utils.escape_markdown(row['reason'])[:1024], inline=False)
        if row['status'] == 'accepted':
            result.add_field(name='Roles', value='Applied' if row['roles_done'] else 'Pending — automatic retry')
        result.set_footer(text=f"Submission DM: {row['submission_dm']} | Decision DM: {row['decision_dm']}")
        return result

    async def dm(self, row, field, message):
        if row[field] != 'pending':
            return
        try:
            user = await self.fetch_user(row['user_id'])
            await user.send(embed=message)
        except (discord.Forbidden, discord.NotFound):
            self.db.update(row['id'], **{field: 'blocked'})
            log.warning('DM unavailable for application %s (%s)', row['id'], field)
        else:
            self.db.update(row['id'], **{field: 'sent'})

    async def process(self, application_id):
        row = self.db.get(application_id)
        if row['guild_id'] != self.guild.id:
            return
        if row['message_id'] is None:
            message = await self.review_channel.send(embed=self.review_embed(row), view=ReviewView(self) if row['status'] == 'pending' else None)
            self.db.update(application_id, message_id=message.id)
        await self.dm(row, 'submission_dm', embed('Application submitted',
                      f"Your request #{application_id} has been successfully submitted. Please wait until staff verify it."))
        row = self.db.get(application_id)
        if row['status'] == 'pending':
            return
        if not row['roles_done']:
            if row['status'] == 'accepted':
                member = await self.guild.fetch_member(row['user_id'])
                await member.add_roles(self.guild.get_role(self.ids['MEMBER_ROLE_ID']), reason=f'Verification #{application_id} accepted')
                await member.remove_roles(self.guild.get_role(self.ids['PROGRESS_ROLE_ID']), reason=f'Verification #{application_id} accepted')
            self.db.update(application_id, roles_done=1)
        description = ('Your verification has been accepted. You now have the Member role. Welcome!'
                       if row['status'] == 'accepted' else
                       f"Your verification was rejected.\n\n**Reason:**\n{discord.utils.escape_markdown(row['reason'])}\n\nYou can submit a new application using Verify.")
        await self.dm(row, 'decision_dm', embed(f"Verification {row['status']}", description))
        row = self.db.get(application_id)
        if not row['review_done']:
            try:
                message = await self.review_channel.fetch_message(row['message_id'])
                await message.edit(embed=self.review_embed(row), view=None)
            except discord.NotFound:
                message = await self.review_channel.send(embed=self.review_embed(row))
                self.db.update(application_id, message_id=message.id)
            self.db.update(application_id, review_done=1)

    async def try_process(self, application_id):
        try:
            await self.process(application_id)
        except discord.HTTPException:
            log.exception('Discord delivery failed for application %s; retrying later', application_id)

    @tasks.loop(seconds=30)
    async def recover(self):
        async with self.lock:
            for row in self.db.work():
                try:
                    await self.try_process(row['id'])
                except Exception:
                    log.exception('Recovery failed for application %s', row['id'])

    async def on_member_join(self, member):
        if member.bot or not self.guild or member.guild.id != self.guild.id:
            return
        try:
            if not any(role.id == self.ids['MEMBER_ROLE_ID'] for role in member.roles):
                await member.add_roles(member.guild.get_role(self.ids['PROGRESS_ROLE_ID']), reason='Awaiting verification')
        except discord.HTTPException:
            log.exception('Could not assign Verify-Progress to member %s', member.id)

    async def close(self):
        if self.recover.is_running():
            self.recover.cancel()
        await super().close()
        self.db.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token or token == 'replace_with_your_bot_token':
        raise SystemExit('Set DISCORD_TOKEN in .env first. See README.md.')
    VerificationBot().run(token, log_handler=None)
