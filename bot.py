import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from database import Database
from time_utils import zurich_time, ZurichFormatter
from views import VerifyView, ReviewView, embed, reply, valid_username

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
    'LOG_CHANNEL_ID': 1562546382772899910,
}


def safe(value, limit=1024):
    return discord.utils.escape_markdown(str(value))[:limit]


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

    def public_context(self, interaction):
        return bool(self.configured and self.guild and interaction.guild_id == self.guild.id
                    and interaction.channel_id == self.ids['VERIFY_CHANNEL_ID'])

    def is_reviewer(self, member):
        return member.guild_permissions.administrator is True or any(r.id == self.ids['STAFF_ROLE_ID'] for r in member.roles)

    def staff_context(self, interaction):
        return bool(self.configured and self.guild and interaction.guild_id == self.guild.id
                    and interaction.channel_id == self.ids['REVIEW_CHANNEL_ID']
                    and isinstance(interaction.user, discord.Member) and self.is_reviewer(interaction.user))

    async def submit(self, interaction, username):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.public_context(interaction):
            return await reply(interaction, 'Not available', 'Use the Verify button in the verification channel.')
        if not valid_username(username):
            return await reply(interaction, 'Invalid username', 'Enter your game username, without links or mentions.')
        async with self.lock:
            member = await self.guild.fetch_member(interaction.user.id)
            if any(r.id == self.ids['MEMBER_ROLE_ID'] for r in member.roles) or self.db.is_verified(self.guild.id, member.id):
                await self.prepare_member(member)
                return await reply(interaction, 'Already verified', 'You already have verified access.')
            await self.prepare_member(member)
            row = self.db.create(self.guild.id, member.id, username)
            if row is None:
                return await reply(interaction, 'Application pending', 'Your request is already waiting for staff review.')
            await self.process(row['id'])
        await reply(interaction, 'Application submitted', 'Your request is saved. Please wait for staff to review it. If DMs are disabled, your request still counts.')

    async def decide(self, interaction, application_id, status, reason=''):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.configured or interaction.guild_id != self.guild.id or interaction.channel_id != self.ids['REVIEW_CHANNEL_ID']:
            return await reply(interaction, 'Not allowed', 'Review requests in verify-check.')
        async with self.lock:
            member = await self.guild.fetch_member(interaction.user.id)
            if not self.is_reviewer(member):
                return await reply(interaction, 'Not allowed', 'Only administrators or High-Rank staff can review requests.')
            row = self.db.get(application_id)
            if not row or row['guild_id'] != self.guild.id:
                return await reply(interaction, 'Application unavailable')
            if not row['review_deleted'] and row['message_id']:
                try:
                    await self.review_channel.fetch_message(row['message_id'])
                except discord.NotFound:
                    self.db.delete_review(application_id)
            try:
                if not self.db.decide(application_id, member.id, status, reason):
                    return await reply(interaction, 'Already handled', 'This application was already decided or removed.')
            except ValueError as error:
                return await reply(interaction, 'Reason required', str(error))
            await self.process(application_id)
            row = self.db.get(application_id)
        description = 'The applicant has been notified, or their DMs are unavailable.'
        if row['decision_dm'] == 'pending':
            description = 'The decision is saved. The remaining updates will retry automatically.'
        await reply(interaction, 'Accepted' if status == 'accepted' else 'Rejected', description)
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

    async def validate_log_channel(self):
        self.log_channel = await self.fetch_channel(self.ids['LOG_CHANNEL_ID'])
        channel = self.log_channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild.id:
            raise RuntimeError('LOG_CHANNEL_ID must be a text channel in the same server.')
        if channel.id in (self.ids['VERIFY_CHANNEL_ID'], self.ids['REVIEW_CHANNEL_ID']):
            raise RuntimeError('Use a separate log channel.')
        if channel.permissions_for(self.guild.default_role).view_channel:
            raise RuntimeError('The log channel must deny View Channel to @everyone.')
        permissions = channel.permissions_for(self.guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.read_message_history)):
            raise RuntimeError('The bot needs View Channel, Send Messages, Embed Links and Read Message History in verified-log.')

    async def ensure_panel(self):
        panel = embed('Police Simulator verification', 'Click Verify and enter your in-game username. Staff will accept or reject your request.')
        message_id = self.db.panel()
        if message_id:
            try:
                message = await self.verify_channel.fetch_message(message_id)
                await message.edit(embed=panel, view=VerifyView(self))
                return
            except discord.NotFound:
                pass
        message = await self.verify_channel.send(embed=panel, view=VerifyView(self))
        self.db.panel(message.id)

    async def on_ready(self):
        async with self.lock:
            if self.configured:
                return
            try:
                await self.validate()
                await self.validate_log_channel()
                await self.ensure_panel()
                tree = app_commands.CommandTree(self)
                try:
                    for command in await tree.fetch_commands(guild=self.guild):
                        if command.name == 'verification-settings':
                            await command.delete()
                except discord.HTTPException:
                    log.warning('Could not remove the retired settings command; verification can still run.')
                for row in self.db.applications():
                    if row['guild_id'] == self.guild.id and not row['review_deleted']:
                        self.db.update(row['id'], review_dirty=True)
                self.configured = True
                self.recover.start()
                log.info('Ready as %s in %s', self.user, self.guild.name)
            except Exception:
                log.exception('Startup failed. Correct the configuration and restart.')
                await self.close()

    def review_embed(self, row):
        result = embed(f"Verification - {row['status'].title()}", f"<@{row['user_id']}>\n**Game username:** {safe(row['username'])}")
        result.add_field(name='Submitted', value=zurich_time(row['created_at']), inline=False)
        if row['reviewer_id']:
            result.add_field(name='Reviewed by', value=f"<@{row['reviewer_id']}>")
        if row['decided_at']:
            result.add_field(name='Decision time', value=zurich_time(row['decided_at']))
        if row['status'] == 'rejected' and row['reason']:
            result.add_field(name='Reason', value=safe(row['reason']), inline=False)
        if row['status'] == 'accepted' and not row['roles_done']:
            result.add_field(name='Roles', value='Updating roles. The bot will retry if necessary.', inline=False)
        result.set_footer(text=f"Application {row['id']}")
        return result

    async def find_message(self, channel, marker, after=None):
        async for message in channel.history(limit=None, after=after):
            if message.author.id == self.user.id and any(e.footer.text == marker for e in message.embeds):
                return message
        return None

    async def sync_review(self, application_id):
        row = self.db.get(application_id)
        if row['review_deleted'] or not row['review_dirty']:
            return
        message = None
        if row['message_id']:
            try:
                message = await self.review_channel.fetch_message(row['message_id'])
            except discord.NotFound:
                self.db.delete_review(application_id)
                return
        else:
            created = datetime.fromisoformat(row['created_at'])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            message = await self.find_message(self.review_channel, f"Application {row['id']}", created - timedelta(seconds=1))
        view = ReviewView(self) if row['status'] == 'pending' else None
        if message:
            await message.edit(embed=self.review_embed(row), view=view)
        else:
            kwargs = dict(embed=self.review_embed(row))
            if view is not None:
                kwargs['view'] = view
            message = await self.review_channel.send(**kwargs)
        self.db.update(application_id, message_id=message.id, review_dirty=False)

    async def apply_acceptance(self, application_id):
        row = self.db.get(application_id)
        if row['status'] != 'accepted' or row['roles_done']:
            return
        member = await self.guild.fetch_member(row['user_id'])
        before = {r.id for r in member.roles}
        member_role = self.ids['MEMBER_ROLE_ID']
        progress_role = self.ids['PROGRESS_ROLE_ID']
        if not row['transfer_started']:
            self.db.update(application_id, transfer_started=True,
                           transfer_eligible=member_role not in before and progress_role in before)
        failure = None
        try:
            if member_role not in before:
                await member.add_roles(self.guild.get_role(member_role), reason='Verification accepted')
            if progress_role in before:
                await member.remove_roles(self.guild.get_role(progress_role), reason='Verification accepted')
        except discord.HTTPException as error:
            failure = error
        current = await self.guild.fetch_member(row['user_id'])
        after = {r.id for r in current.roles}
        if member_role in after and progress_role not in after:
            self.db.finish_acceptance(application_id)
        elif failure:
            raise failure
        else:
            log.warning('Roles not yet confirmed for application %s; will retry', application_id)

    async def prepare_member(self, member):
        unfinished = next((row for row in self.db.applications() if row['guild_id'] == self.guild.id
                           and row['user_id'] == member.id and row['status'] == 'accepted' and not row['roles_done']), None)
        if unfinished:
            await self.apply_acceptance(unfinished['id'])
            return
        roles = {r.id for r in member.roles}
        member_role = self.ids['MEMBER_ROLE_ID']
        progress_role = self.ids['PROGRESS_ROLE_ID']
        verified = member_role in roles or self.db.is_verified(self.guild.id, member.id)
        if verified:
            if member_role not in roles:
                await member.add_roles(self.guild.get_role(member_role), reason='Restore verified access')
            if progress_role in roles:
                await member.remove_roles(self.guild.get_role(progress_role), reason='Already verified')
        elif progress_role not in roles:
            await member.add_roles(self.guild.get_role(progress_role), reason='Awaiting verification')

    async def send_dm(self, application_id, field):
        row = self.db.get(application_id)
        if row[field] != 'pending':
            return
        if field == 'submission_dm':
            text = 'Your request has been successfully submitted. Please wait until staff verify it.'
        elif row['status'] == 'accepted':
            if not row['roles_done']:
                return
            text = 'Your verification has been accepted. You now have the Member role. Welcome!'
        elif row['status'] == 'rejected':
            text = f"Your verification was rejected.\n\n**Reason:** {safe(row['reason'])}\n\nYou may submit again."
        elif row['status'] == 'cancelled':
            text = 'Your pending request was removed by staff. You may submit a new request.'
        else:
            return
        try:
            user = await self.fetch_user(row['user_id'])
            await user.send(embed=embed('Police Simulator verification', text))
        except (discord.Forbidden, discord.NotFound):
            self.db.update(application_id, **{field: 'blocked'})
        else:
            self.db.update(application_id, **{field: 'done'})

    def history_embed(self, history):
        result = embed('Verification history', f"<@{history['user_id']}>\nVerify-Process removed and Member added.")
        entries = history['entries'][-8:]
        for entry in entries:
            result.add_field(name=zurich_time(entry['at']),
                value=f"**Username:** {safe(entry['username'], 200)}\n**Accepted by:** <@{entry['reviewer_id']}>", inline=False)
        result.set_footer(text=f"Verification history {history['user_id']} | {len(history['entries'])} completed")
        return result

    async def sync_history(self, history):
        if history['guild_id'] != self.guild.id or not history['dirty'] or history['deleted']:
            return
        message = None
        if history['message_id']:
            try:
                message = await self.log_channel.fetch_message(history['message_id'])
            except discord.NotFound:
                self.db.update_history(self.guild.id, history['user_id'], deleted=True, dirty=False)
                return
        else:
            async for candidate in self.log_channel.history(limit=None):
                if candidate.author.id == self.user.id and any(
                    (e.footer.text or '').startswith(f"Verification history {history['user_id']} |") for e in candidate.embeds):
                    message = candidate
                    break
        if message:
            await message.edit(embed=self.history_embed(history))
        else:
            message = await self.log_channel.send(embed=self.history_embed(history))
        self.db.update_history(self.guild.id, history['user_id'], message_id=message.id, dirty=False)

    async def attempt(self, action, *args):
        try:
            await action(*args)
        except (discord.HTTPException, OSError):
            log.exception('%s failed; saved work will retry', action.__name__)

    async def process(self, application_id):
        row = self.db.get(application_id)
        if row['guild_id'] != self.guild.id:
            return
        await self.attempt(self.sync_review, application_id)
        await self.attempt(self.apply_acceptance, application_id)
        await self.attempt(self.sync_review, application_id)
        await self.attempt(self.send_dm, application_id, 'submission_dm')
        await self.attempt(self.send_dm, application_id, 'decision_dm')

    @tasks.loop(seconds=30)
    async def recover(self):
        async with self.lock:
            for row in self.db.applications():
                if row['guild_id'] == self.guild.id:
                    try:
                        await self.process(row['id'])
                    except Exception:
                        log.exception('Recovery failed for application %s', row['id'])
            for history in self.db.histories():
                await self.attempt(self.sync_history, history)
            for job in self.db.joins():
                if job['guild_id'] != self.guild.id:
                    continue
                try:
                    member = await self.guild.fetch_member(job['user_id'])
                    await self.prepare_member(member)
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    log.exception('Join role failed; will retry')
                    continue
                self.db.finish_join(job['guild_id'], job['user_id'])

    async def on_member_join(self, member):
        if member.bot or not self.configured or member.guild.id != self.guild.id:
            return
        async with self.lock:
            self.db.queue_join(self.guild.id, member.id)
            try:
                await self.prepare_member(member)
                self.db.finish_join(self.guild.id, member.id)
            except discord.HTTPException:
                log.exception('Join role failed; will retry')

    async def delete_messages(self, guild_id, message_ids):
        if not self.configured or guild_id != self.guild.id:
            return
        async with self.lock:
            for row in self.db.applications():
                if row['guild_id'] == guild_id and row['message_id'] in message_ids and not row['review_deleted']:
                    self.db.delete_review(row['id'])
            for history in self.db.histories():
                if history['guild_id'] == guild_id and history['message_id'] in message_ids:
                    self.db.update_history(guild_id, history['user_id'], deleted=True, dirty=False)

    async def on_raw_message_delete(self, payload):
        await self.delete_messages(payload.guild_id, {payload.message_id})

    async def on_raw_bulk_message_delete(self, payload):
        await self.delete_messages(payload.guild_id, payload.message_ids)

    async def close(self):
        if self.recover.is_running():
            self.recover.cancel()
        await super().close()


def main():
    handler = logging.StreamHandler()
    handler.setFormatter(ZurichFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    token = os.getenv('DISCORD_TOKEN', '').strip()
    if not token or token == 'replace_with_your_bot_token':
        raise SystemExit('Set DISCORD_TOKEN in .env first. See README.md.')
    VerificationBot().run(token, log_handler=None)


if __name__ == '__main__':
    main()
