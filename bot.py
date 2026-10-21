import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from database import Database
from language import tr
from views import VerifyView, ReviewView, embed, reply

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


def safe(value, limit=1024):
    return discord.utils.escape_markdown(str(value))[:limit] or '-'


class VerificationBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.ids = {key: int(os.getenv(key, value)) for key, value in DEFAULTS.items()}
        self.db = Database(os.getenv('DATABASE_PATH', 'data/verification.json'))
        if self.db.setting('queue_limit') is None:
            self.db.set_setting('queue_limit', max(0, int(os.getenv('QUEUE_LIMIT', '40'))))
        self.lock = asyncio.Lock()
        self.guild = None
        self.configured = False
        self.tree = app_commands.CommandTree(self)

        @self.tree.command(name='verification-settings', description='Pause or resume applications and set the queue limit')
        @app_commands.guild_only()
        async def settings(interaction: discord.Interaction, paused: bool, queue_limit: app_commands.Range[int, 0, 1000], reason: str = ''):
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not await self.staff_allowed(interaction):
                return
            async with self.lock:
                try:
                    self.db.configure(interaction.user.id, paused, reason[:800], queue_limit)
                except ValueError as error:
                    return await reply(interaction, 'Invalid settings', str(error))
                await self.ensure_panel()
            await reply(interaction, 'Settings saved', f'Paused: {paused}\nQueue limit: {queue_limit} (0 means unlimited)')

        @self.tree.error
        async def command_error(interaction, error):
            log.error('Command failed', exc_info=(type(error), error, error.__traceback__))
            await reply(interaction, 'Unable to complete action', 'Check your permissions and try again.')

    def rules(self, language):
        default = ('Follow the server rules, treat others respectfully, and provide accurate application information.'
                   if language == 'en' else
                   'Befolge die Serverregeln, behandle andere respektvoll und mache korrekte Angaben im Antrag.')
        return os.getenv(f'RULES_{language.upper()}', default)[:3500]

    def welcome(self, language):
        default = ('Welcome to the community. Read the server announcements and ask High-Rank staff how to join your first patrol.'
                   if language == 'en' else
                   'Willkommen in der Community. Lies die Ankuendigungen und frage das High-Rank-Team nach deiner ersten Streife.')
        return os.getenv(f'WELCOME_{language.upper()}', default)[:1500]

    async def setup_hook(self):
        self.add_view(VerifyView(self))
        self.add_view(ReviewView(self))

    def public_context(self, interaction):
        return bool(self.configured and self.guild and interaction.guild_id == self.guild.id
                    and interaction.channel_id == self.ids['VERIFY_CHANNEL_ID'])

    def staff_context(self, interaction):
        return bool(self.configured and self.guild and interaction.guild_id == self.guild.id
                    and interaction.channel_id == self.ids['REVIEW_CHANNEL_ID']
                    and isinstance(interaction.user, discord.Member)
                    and any(r.id == self.ids['STAFF_ROLE_ID'] for r in interaction.user.roles))

    async def staff_allowed(self, interaction):
        if not self.configured or interaction.guild_id != self.guild.id or interaction.channel_id != self.ids['REVIEW_CHANNEL_ID']:
            await reply(interaction, 'Not allowed', 'Use this action in verify-check.')
            return False
        member = await self.guild.fetch_member(interaction.user.id)
        if not any(r.id == self.ids['STAFF_ROLE_ID'] for r in member.roles):
            await reply(interaction, 'Not allowed', 'Only the configured High-Rank role may review applications.')
            return False
        return True

    async def applicant_allowed(self, interaction, language='en'):
        if not self.public_context(interaction):
            await reply(interaction, tr(language, 'error'), tr(language, 'retry'))
            return False
        member = await self.guild.fetch_member(interaction.user.id)
        latest = self.db.latest(interaction.guild_id, member.id)
        error = None
        if any(r.id == self.ids['MEMBER_ROLE_ID'] for r in member.roles) or (latest and latest['status'] == 'accepted'):
            error = tr(language, 'verified')
        elif latest and latest['status'] == 'pending':
            error = tr(language, 'duplicate')
        elif self.db.setting('paused'):
            error = tr(language, 'paused') + '\n' + safe(self.db.setting('pause_reason'), 800)
        else:
            limit = self.db.setting('queue_limit') or 0
            count = sum(r['status'] == 'pending' and r['guild_id'] == self.guild.id for r in self.db.data['applications'].values())
            if limit and count >= limit:
                error = tr(language, 'full')
        if error:
            await reply(interaction, tr(language, 'error'), error)
        return error is None

    async def apply_action(self, interaction, row, action, detail='', applicant=False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        language = row['language'] if applicant else 'en'
        async with self.lock:
            if applicant:
                if not self.public_context(interaction) or interaction.user.id != row['user_id']:
                    return await reply(interaction, tr(language, 'error'), tr(language, 'retry'))
            elif not await self.staff_allowed(interaction):
                return
            if row['guild_id'] != interaction.guild_id:
                return await reply(interaction, 'Not allowed')
            try:
                self.db.transition(row['id'], interaction.user.id, action, row['revision'], detail)
            except ValueError as error:
                return await reply(interaction, tr(language, 'error'), tr(language, 'stale') if applicant else str(error))
            await self.try_process(row['id'])
        await reply(interaction, tr(language, 'saved'),
                    tr(language, action) if applicant else 'The action is saved. Any unfinished role or message updates will retry automatically.')
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

    async def setup_logs(self):
        channel_id = int(os.getenv('LOG_CHANNEL_ID', '1562546382772899910')) or self.db.setting('log_channel')
        channel = None
        if channel_id:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.NotFound:
                if int(os.getenv('LOG_CHANNEL_ID', '1562546382772899910')):
                    raise RuntimeError('Configured LOG_CHANNEL_ID does not exist.')
        if channel is None:
            if not self.guild.me.guild_permissions.manage_channels:
                raise RuntimeError('Set LOG_CHANNEL_ID to a private text channel or grant Manage Channels to create verification-logs.')
            overwrites = {
                self.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                self.guild.get_role(self.ids['STAFF_ROLE_ID']): discord.PermissionOverwrite(view_channel=True, read_message_history=True),
                self.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True),
            }
            channel = await self.guild.create_text_channel('verification-logs',
                category=self.guild.get_channel(self.ids['STAFF_CATEGORY_ID']), overwrites=overwrites,
                reason='Private verification audit log')
            self.db.set_setting('log_channel', channel.id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild.id:
            raise RuntimeError('LOG_CHANNEL_ID must be a text channel in this server.')
        if channel.id in (self.ids['VERIFY_CHANNEL_ID'], self.ids['REVIEW_CHANNEL_ID']):
            raise RuntimeError('Use a dedicated channel for verification logs.')
        if channel.permissions_for(self.guild.default_role).view_channel:
            raise RuntimeError('Verification log channel must deny View Channel to @everyone.')
        permissions = channel.permissions_for(self.guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links)):
            raise RuntimeError('Bot cannot send embeds in the log channel.')
        self.log_channel = channel

    async def ensure_panel(self):
        description = ('Click Verify for English or Deutsch for German.\n'
                       'Read the rules, submit your game username and wait for staff review.\n'
                       'Use My application to check updates, answer questions or withdraw.')
        if self.db.setting('paused'):
            description += '\n\nApplications paused / Antraege pausiert:\n' + safe(self.db.setting('pause_reason'), 800)
        panel = embed('Police Simulator verification', description)
        panel_id = self.db.setting('panel_message')
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
                await self.setup_logs()
                await self.ensure_panel()
                self.tree.copy_global_to(guild=self.guild)
                await self.tree.sync(guild=self.guild)
                for row in list(self.db.data['applications'].values()):
                    if row['guild_id'] == self.guild.id:
                        self.db.update(row['id'], review_done=0)
                self.configured = True
                async for member in self.guild.fetch_members(limit=None):
                    if not member.bot:
                        self.db.track_member(self.guild.id, member.id,
                            any(r.id == self.ids['MEMBER_ROLE_ID'] for r in member.roles))
                self.recover.start()
                log.info('Ready as %s in %s', self.user, self.guild.name)
            except Exception:
                log.exception('Startup failed. Correct configuration and restart.')
                await self.close()

    def applicant_embed(self, row):
        language = row['language']
        result = embed(tr(language, 'title'), tr(language, row['status']))
        result.add_field(name=tr(language, 'username'), value=safe(row['username']), inline=False)
        if row['reason']:
            result.add_field(name=tr(language, 'reason'), value=safe(row['reason']), inline=False)
        if row['question']:
            result.add_field(name=tr(language, 'question')[:256], value=safe(row['question']), inline=False)
        if row['answer']:
            result.add_field(name=tr(language, 'reply'), value=safe(row['answer']), inline=False)
        if row['status'] == 'accepted' and row['roles_done']:
            result.add_field(name=tr(language, 'welcome'), value=self.welcome(language)[:1024], inline=False)
        if row['status'] == 'accepted' and not row['roles_done']:
            result.description = ('Accepted. Role assignment is pending.' if language == 'en'
                                  else 'Angenommen. Die Rollenzuweisung steht noch aus.')
        result.set_footer(text=f"Application {row['id']}")
        return result

    def review_embed(self, row):
        result = embed(f"Verification {row['id']} - {row['status'].title()}", f"Applicant: <@{row['user_id']}>\nDiscord ID: {row['user_id']}")
        for name, value in [('Game username', row['username']), ('Language', row['language']),
                            ('Submitted UTC', row['created_at']), ('Claimed by', row['claimed_by']),
                            ('Rules agreed', row['rules']), ('Patrol availability', row['answers'].get('patrol')),
                            ('Experience', row['answers'].get('experience')), ('Staff question', row['question']),
                            ('Applicant answer', row['answer']), ('Reason / note', row['reason']),
                            ('Reviewer', row['reviewer_id'])]:
            if value is not None:
                result.add_field(name=name, value=safe(value, 450), inline=False)
        duplicates = self.db.duplicates(row)
        if duplicates:
            result.add_field(name='Duplicate username warning', value=safe(', '.join(map(str, sorted(set(duplicates)))), 500), inline=False)
        result.set_footer(text=f"Revision {row['revision']} | Role update {'complete' if row['roles_done'] else 'pending'}")
        return result

    async def sync_review(self, application_id):
        row = self.db.get(application_id)
        if row['review_done'] and row['message_id']:
            return
        message = None
        if row['message_id']:
            try:
                message = await self.review_channel.fetch_message(row['message_id'])
            except discord.NotFound:
                pass
        if message:
            await message.edit(embed=self.review_embed(row), view=ReviewView(self))
        else:
            message = await self.review_channel.send(embed=self.review_embed(row), view=ReviewView(self))
        self.db.update(application_id, message_id=message.id, review_done=1)

    async def sync_roles(self, state):
        if state['guild_id'] != self.guild.id or not state['dirty']:
            return
        member = await self.guild.fetch_member(state['user_id'])
        grant = 'MEMBER_ROLE_ID' if state['verified'] else 'PROGRESS_ROLE_ID'
        remove = 'PROGRESS_ROLE_ID' if state['verified'] else 'MEMBER_ROLE_ID'
        await member.add_roles(self.guild.get_role(self.ids[grant]), reason='Verification state update')
        await member.remove_roles(self.guild.get_role(self.ids[remove]), reason='Verification state update')
        self.db.roles_applied(self.guild.id, state['user_id'])

    async def deliver_event(self, event):
        row = self.db.get(event['application_id']) if event['application_id'] else None
        if row and row['guild_id'] != self.guild.id:
            return
        action = event['action']
        if not event['audit_done']:
            try:
                await self.log_channel.send(embed=embed(f"Verification event {event['id']}: {action}",
                    f"Application: {event['application_id']}\nApplicant: {row['user_id'] if row else '-'}\nActor: {event['actor'] or 'Bot'}\nUTC: {event['at']}\n{safe(event['detail'], 1800)}"))
                self.db.event_done(event['id'], 'audit_done')
            except discord.HTTPException:
                log.exception('Audit delivery failed; will retry')
        if event['dm_done']:
            return
        notify = action in ('submitted', 'accepted', 'rejected', 'cancelled', 'revoked', 'undo', 'question')
        if row:
            expected = {'undo': 'pending', 'question': 'pending', 'accepted': 'accepted',
                        'rejected': 'rejected', 'cancelled': 'cancelled', 'revoked': 'revoked'}
            if action in expected and (row['status'] != expected[action] or event.get('revision') != row['revision']):
                notify = False
            if action == 'question' and (row['question'] != event['detail'] or row['answer'] is not None):
                notify = False
            if action == 'accepted' and notify and not row['roles_done']:
                return
        if notify and row:
            language = row['language']
            description = tr(language, action)
            if event['detail']:
                description += '\n\n' + safe(event['detail'], 1600)
            if action == 'accepted':
                description += '\n\n' + self.welcome(language)
            description += f"\n\n<#{self.ids['VERIFY_CHANNEL_ID']}>"
            try:
                user = await self.fetch_user(row['user_id'])
                await user.send(embed=embed(tr(language, 'title'), description))
            except (discord.Forbidden, discord.NotFound):
                log.warning('DM unavailable for application %s', row['id'])
            except discord.HTTPException:
                log.exception('DM failed; will retry')
                return
        self.db.event_done(event['id'], 'dm_done')

    async def process(self, application_id):
        row = self.db.get(application_id)
        if row['guild_id'] != self.guild.id:
            return
        state = self.db.member_state(row['guild_id'], row['user_id'])
        if state:
            try:
                await self.sync_roles(state)
            except discord.HTTPException:
                log.exception('Role update failed; will retry')
        await self.sync_review(application_id)
        for event in self.db.events():
            if event['application_id'] == application_id and (not event['audit_done'] or not event['dm_done']):
                await self.deliver_event(event)

    async def try_process(self, application_id):
        try:
            await self.process(application_id)
        except discord.HTTPException:
            log.exception('Application delivery failed; will retry')

    @tasks.loop(seconds=30)
    async def recover(self):
        async with self.lock:
            for state in self.db.members():
                if state['dirty'] and state['guild_id'] == self.guild.id:
                    try:
                        await self.sync_roles(state)
                    except Exception:
                        log.exception('Role recovery failed for %s', state['user_id'])
            for row in list(self.db.data['applications'].values()):
                if row['guild_id'] == self.guild.id and not row['review_done']:
                    try:
                        await self.sync_review(row['id'])
                    except Exception:
                        log.exception('Review recovery failed for %s', row['id'])
            for event in self.db.events():
                if not event['audit_done'] or not event['dm_done']:
                    try:
                        await self.deliver_event(event)
                    except Exception:
                        log.exception('Event recovery failed for %s', event['id'])

    async def on_member_join(self, member):
        if member.bot or not self.configured or member.guild.id != self.guild.id:
            return
        async with self.lock:
            self.db.track_member(member.guild.id, member.id)
            try:
                await self.sync_roles(self.db.member_state(member.guild.id, member.id))
            except discord.HTTPException:
                log.exception('Join role update failed; will retry')

    async def on_raw_message_delete(self, payload):
        if not self.configured or payload.guild_id != self.guild.id:
            return
        async with self.lock:
            if payload.message_id == self.db.setting('panel_message'):
                await self.ensure_panel()
            row = self.db.by_message(payload.message_id)
            if row:
                self.db.update(row['id'], message_id=None, review_done=0)

    async def close(self):
        if self.recover.is_running():
            self.recover.cancel()
        await super().close()
        self.db.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token or token == 'replace_with_your_bot_token':
        raise SystemExit('Set DISCORD_TOKEN in .env first. See README.md.')
    VerificationBot().run(token, log_handler=None)


if __name__ == '__main__':
    main()
