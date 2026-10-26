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
from views import VerifyView, ReviewView, PauseControls, embed, reply, send_embed, valid_username
from identity import nickname, profile_from_member, clean_clan, valid_clan

load_dotenv()
log = logging.getLogger('verification')
DEFAULTS = {
    'VERIFY_CATEGORY_ID': 1562540102394060840,
    'VERIFY_CHANNEL_ID': 1562540152276910093,
    'STAFF_ROLE_ID': 1562535759276417065,
    'PROGRESS_ROLE_ID': 1562535216050864209,
    'ALLIES_ROLE_ID': 1562903902662168587,
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
        self.db = Database(os.getenv('DATABASE_PATH', 'data/verification.json'), os.getenv('REJECTION_COOLDOWN_SECONDS', '600'))
        self.lock = asyncio.Lock()
        self.guild = None
        self.configured = False
        self.prefix_task = None

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

    def verified_role_ids(self):
        return {self.ids['MEMBER_ROLE_ID'], self.ids['ALLIES_ROLE_ID']}

    def target_role(self, row):
        return self.ids['ALLIES_ROLE_ID' if row.get('kind') == 'allies' else 'MEMBER_ROLE_ID']

    async def submit(self, interaction, username, kind='member', clan=''):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.public_context(interaction):
            return await reply(interaction, 'Not available', 'Use the Verify button in the verification channel.')
        if kind not in ('member', 'allies'):
            return await reply(interaction, 'Invalid selection', 'Choose Member or Allies.')
        if not valid_username(username):
            return await reply(interaction, 'Invalid username', 'Enter your game username, without links or mentions.')
        clan = 'SaS' if kind == 'member' else clean_clan(clan)
        error = self.profile_error(dict(kind=kind, username=username, clan=clan))
        if error:
            return await reply(interaction, 'Check your details', error)
        async with self.lock:
            if self.db.paused()['active']:
                return await reply(interaction, 'Applications paused', safe(self.db.paused()['reason']))
            member = await self.guild.fetch_member(interaction.user.id)
            if any(r.id in self.verified_role_ids() for r in member.roles) or self.db.is_verified(self.guild.id, member.id):
                await self.prepare_member(member)
                return await reply(interaction, 'Already verified', 'You already have verified access.')
            until = self.db.cooldown_until(self.guild.id, member.id)
            if until:
                return await reply(interaction, 'Please wait before reapplying', f'You can submit another request at {zurich_time(until.isoformat())}.')
            row = self.db.create(self.guild.id, member.id, username, kind, clan)
            if row is None:
                return await reply(interaction, 'Application pending', 'Your request is already waiting for staff review.')
            await self.attempt(self.prepare_member, member)
            await self.process(row['id'])
        await reply(interaction, 'Application submitted', 'Your request is saved. Please wait for staff to review it. If DMs are disabled, your request still counts.')

    @staticmethod
    def profile_error(profile):
        if not valid_username(profile['username']) or any(c in profile['username'] for c in '[]'):
            return 'Enter only your in-game username, without a clan tag, links or mentions.'
        if profile['clan'] and not valid_clan(profile['clan']):
            return 'Clan names must be 1 to 12 characters using letters, numbers, spaces, hyphens or underscores.'
        if profile['kind'] == 'allies' and not profile['clan']:
            return 'Enter your clan name.'
        prefix = len(profile['clan']) + 3 if profile['clan'] else 0
        if len(profile['username']) + prefix > 32:
            return f"With this clan tag, your username can contain at most {32 - prefix} characters. Discord nicknames allow 32 characters in total."
        return None

    async def submit_change(self, interaction, operation, value=''):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.public_context(interaction):
            return await reply(interaction, 'Not available', 'Use the verification channel.')
        if operation not in ('name', 'clan', 'join_member'):
            return await reply(interaction, 'Invalid request')
        async with self.lock:
            member = await self.guild.fetch_member(interaction.user.id)
            previous = profile_from_member(member, self.ids, self.db.profile(self.guild.id, member.id))
            if not previous:
                return await reply(interaction, 'Verification required', 'Get verified as a Member or Ally before requesting a change.')
            if self.db.busy(self.guild.id, member.id):
                return await reply(interaction, 'Request already in progress', 'Wait until staff decide your request and its updates finish.')
            proposed = dict(previous)
            if operation == 'name':
                proposed['username'] = value.strip()
            elif operation == 'join_member':
                proposed.update(kind='member', clan='SaS')
            else:
                proposed.update(kind='allies', clan=clean_clan(value))
                if proposed['clan'] == 'SaS':
                    return await reply(interaction, 'Joining SaS', 'Use Clan change and choose Yes - Join SaS to request Member verification.')
            if proposed == previous:
                return await reply(interaction, 'Nothing to change', 'These details already match your current profile.')
            error = self.profile_error(proposed)
            if error:
                return await reply(interaction, 'Check your details', error)
            try:
                row = self.db.create(self.guild.id, member.id, proposed['username'], proposed['kind'], proposed['clan'], operation, previous)
            except ValueError as error:
                return await reply(interaction, 'Request unavailable', str(error))
            if row is None:
                return await reply(interaction, 'Request already in progress')
            self.db.save_profile(self.guild.id, member.id, previous)
            await self.process(row['id'])
        await reply(interaction, 'Change request submitted', f"Requested nickname: **{safe(nickname(proposed))}**. Staff must approve before anything changes.")

    async def can_pause(self, interaction):
        if not self.public_context(interaction):
            return False
        member = await self.guild.fetch_member(interaction.user.id)
        return any(role.id == self.ids['STAFF_ROLE_ID'] for role in member.roles)

    async def open_staff_controls(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self.can_pause(interaction):
            return await reply(interaction, 'High-Rank role required', 'Only the configured High-Rank role can pause or resume applications.')
        pause = self.db.paused()
        await send_embed(interaction, embed('Verification availability',
            'Applications are paused.\n' + safe(pause['reason']) if pause['active'] else 'Applications are open.'), PauseControls(self))

    async def set_application_pause(self, interaction, active, reason=''):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.lock:
            if not await self.can_pause(interaction):
                return await reply(interaction, 'High-Rank role required', 'Only the configured High-Rank role can pause or resume applications.')
            if active and not reason.strip():
                return await reply(interaction, 'Explanation required', 'Tell applicants why applications are paused.')
            self.db.set_pause(active, reason[:500], interaction.user.id)
            await self.attempt(self.ensure_panel)
        await reply(interaction, 'Applications paused' if active else 'Applications resumed',
            'The setting is saved. The public panel will retry updating if Discord is temporarily unavailable.' if self.db.paused()['dirty'] else 'The public panel has been updated.')

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
        description = 'Result DM sent successfully.' if row['decision_dm'] == 'done' else 'Result DM could not be delivered. The applicant may have DMs disabled.'
        if row['decision_dm'] == 'pending':
            description = 'The decision is saved. The remaining updates will retry automatically.'
        duplicates = self.db.duplicates(row)
        if duplicates:
            description += '\n\nDuplicate game name on file for: ' + ', '.join(f'<@{identifier}>' for identifier in duplicates[:15])
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
        if len({self.ids[k] for k in ('PROGRESS_ROLE_ID', 'MEMBER_ROLE_ID', 'ALLIES_ROLE_ID')}) != 3:
            raise RuntimeError('Progress, Member and Allies must be different roles.')
        if not self.guild.me.guild_permissions.manage_nicknames:
            log.warning('Manage Nicknames is missing; acceptance will work but nickname changes may fail.')
        roles = {role.id: role for role in await self.guild.fetch_roles()}
        for key in ('STAFF_ROLE_ID', 'PROGRESS_ROLE_ID', 'MEMBER_ROLE_ID', 'ALLIES_ROLE_ID'):
            if self.ids[key] not in roles:
                raise RuntimeError(f'Role {key} does not exist.')
        if not self.guild.me.guild_permissions.manage_roles:
            raise RuntimeError('The bot needs Manage Roles permission.')
        for key in ('PROGRESS_ROLE_ID', 'MEMBER_ROLE_ID', 'ALLIES_ROLE_ID'):
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

    def panel_embed(self):
        pause = self.db.paused()
        panel = embed('SaS | Verification Centre',
            'A shared patrol starts with a proper introduction.\nChoose your access below to join our **Police Simulator** community.')
        panel.color = 0xD4A85A if pause['active'] else 0x183B56
        icon = self.guild.icon
        if icon:
            panel.set_thumbnail(url=str(icon.url))
        panel.add_field(name='01 / SaS MEMBERS',
            value=f"For players joining SaS.\n**Verify - Member**\nAccess: <@&{self.ids['MEMBER_ROLE_ID']}>\nNickname: `[SaS] Username`", inline=True)
        panel.add_field(name='02 / ALLIED CLANS',
            value=f"For players representing another clan.\n**Verify - Allies**\nAccess: <@&{self.ids['ALLIES_ROLE_ID']}>\nNickname: `[Clan] Username`", inline=True)
        panel.add_field(name='YOUR APPLICATION', value='**Choose your route**  /  **Enter your details**  /  **Wait for approval**\nStaff review every request. Your result arrives by DM; keep server DMs enabled.', inline=False)
        panel.add_field(name='ALREADY VERIFIED?', value='**Name change** updates your game name while keeping your clan.\n**Clan change** lets you join SaS or move to another clan.\nAll changes require staff approval. Your current access stays until a decision.', inline=False)
        panel.add_field(name='APPLICATIONS PAUSED' if pause['active'] else 'APPLICATIONS OPEN',
            value=safe(pause['reason']) if pause['active'] else 'Member and Allies applications are welcome. One request at a time.', inline=False)
        panel.set_footer(text='SaS community | Staff approval required | No instant role changes')
        return panel

    async def ensure_panel(self):
        panel = self.panel_embed()
        message_id = self.db.panel()
        if message_id:
            try:
                message = await self.verify_channel.fetch_message(message_id)
                await message.edit(embed=panel, view=VerifyView(self))
                self.db.panel_synced()
                return
            except discord.NotFound:
                pass
        message = await self.verify_channel.send(embed=panel, view=VerifyView(self))
        self.db.panel(message.id)
        self.db.panel_synced()

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
                self.prefix_task = asyncio.create_task(self.discover_member_prefixes())
                log.info('Ready as %s in %s', self.user, self.guild.name)
            except Exception:
                log.exception('Startup failed. Correct the configuration and restart.')
                await self.close()

    def review_embed(self, row):
        status = row['status']
        colors = {'pending': 0xD69E2E, 'accepted': 0x258568, 'rejected': 0xBF4545, 'cancelled': 0x64748B}
        label = {'verification': row['kind'].title() + ' verification', 'name': 'Name change', 'clan': 'Clan change', 'join_member': 'Member verification'}[row['operation']]
        result = embed(f"{label} / {status.title()}",
                       f"<@{row['user_id']}>\nApplication **{row['id']:04d}**")
        result.color = colors.get(status, 0x64748B)
        result.add_field(name='GAME USERNAME', value=safe(row['username']), inline=True)
        result.add_field(name='ACCESS REQUESTED', value=f"<@&{self.target_role(row)}>", inline=True)
        result.add_field(name='CLAN', value=safe(row['clan']) or 'Not recorded', inline=True)
        result.add_field(name='REQUESTED NICKNAME', value=safe(nickname(row)), inline=False)
        if row['previous']:
            result.add_field(name='CURRENT PROFILE', value=f"{safe(nickname(row['previous']))} / {row['previous']['kind'].title()}", inline=False)
            if row['previous']['kind'] != row['kind']:
                result.add_field(name='ROLE CHANGE ON APPROVAL', value=f"Add {row['kind'].title()}; remove {row['previous']['kind'].title()}.", inline=False)
        result.add_field(name='SUBMITTED / ZURICH', value=zurich_time(row['created_at']), inline=False)
        duplicates = self.db.duplicates(row)
        if duplicates:
            names = ', '.join(f'<@{identifier}>' for identifier in duplicates[:15])
            result.add_field(name='DUPLICATE USERNAME', value=f'This name is already registered to: {names}. Staff should check before accepting.', inline=False)
        if status == 'pending':
            result.add_field(name='STAFF REVIEW', value='Check the game name and requested access, then accept or reject below. Rejection requires a reason.', inline=False)
        if row['reviewer_id']:
            result.add_field(name='REVIEWED BY', value=f"<@{row['reviewer_id']}>")
        if row['decided_at']:
            result.add_field(name='DECISION / ZURICH', value=zurich_time(row['decided_at']))
        if status == 'rejected':
            result.add_field(name='REASON', value=safe(row['reason']), inline=False)
            if row.get('retry_after'):
                result.add_field(name='CAN REAPPLY / ZURICH', value=zurich_time(row['retry_after']), inline=False)
        if status == 'accepted':
            access = 'Existing access retained.' if row['operation'] == 'name' else 'Role update confirmed.'
            result.add_field(name='ACCESS', value=access if row['roles_done'] else 'Updating request. Unfinished changes retry automatically.', inline=False)
            notes = {'done': f"Set to **{safe(nickname(row))}**.",
                     'blocked': 'Could not change nickname. Staff can set it manually; check Manage Nicknames and role hierarchy.',
                     'pending': 'Nickname update pending.', 'legacy': 'Existing verification; nickname preserved.'}
            result.add_field(name='SERVER NICKNAME', value=notes[row['nickname_state']], inline=False)
        delivery = {'pending': 'Pending / will retry', 'done': 'Sent successfully', 'blocked': 'Delivery failed / DMs unavailable'}
        result.add_field(name='SUBMISSION DM', value=delivery[row['submission_dm']], inline=True)
        result.add_field(name='RESULT DM', value='Waiting for a staff decision' if status == 'pending' else delivery[row['decision_dm']], inline=True)
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
        if row['operation'] == 'name':
            self.db.finish_acceptance(application_id)
            return
        member_role = self.target_role(row)
        progress_role = self.ids['PROGRESS_ROLE_ID']
        if not row['transfer_started']:
            self.db.update(application_id, transfer_started=True,
                           transfer_eligible=member_role not in before and progress_role in before)
        previous_role = self.target_role(row['previous']) if row['previous'] and row['previous']['kind'] != row['kind'] else None
        failure = None
        try:
            if member_role not in before:
                await member.add_roles(self.guild.get_role(member_role), reason='Verification accepted')
            if previous_role and previous_role in before:
                await member.remove_roles(self.guild.get_role(previous_role), reason='Approved clan change')
            if progress_role in before:
                await member.remove_roles(self.guild.get_role(progress_role), reason='Verification accepted')
        except discord.HTTPException as error:
            failure = error
        current = await self.guild.fetch_member(row['user_id'])
        after = {r.id for r in current.roles}
        if member_role in after and progress_role not in after and (not previous_role or previous_role not in after):
            self.db.finish_acceptance(application_id)
        elif failure:
            raise failure
        else:
            log.warning('Roles not yet confirmed for application %s; will retry', application_id)

    async def apply_nickname(self, application_id):
        row = self.db.get(application_id)
        if row['status'] != 'accepted' or not row['roles_done'] or row['nickname_state'] != 'pending':
            return
        member = await self.guild.fetch_member(row['user_id'])
        requested_nickname = nickname(row)
        state = 'done'
        try:
            if member.nick != requested_nickname:
                await member.edit(nick=requested_nickname, reason='Verification accepted')
            current = await self.guild.fetch_member(row['user_id'])
            if current.nick != requested_nickname:
                return
        except discord.Forbidden:
            state = 'blocked'
        except discord.HTTPException as error:
            if error.status != 400:
                raise
            state = 'blocked'
        self.db.update(application_id, nickname_state=state, review_dirty=not row['review_deleted'])

    async def prepare_member(self, member):
        unfinished = next((row for row in self.db.applications() if row['guild_id'] == self.guild.id
                           and row['user_id'] == member.id and row['status'] == 'accepted' and not row['roles_done']), None)
        if unfinished:
            await self.apply_acceptance(unfinished['id'])
            if unfinished['operation'] != 'name':
                return
        roles = {r.id for r in member.roles}
        member_role = self.target_role({'kind': self.db.verified_kind(self.guild.id, member.id)})
        progress_role = self.ids['PROGRESS_ROLE_ID']
        verified = bool(roles & self.verified_role_ids()) or self.db.is_verified(self.guild.id, member.id)
        if verified:
            if not roles & self.verified_role_ids():
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
            if not row['roles_done'] or not row['profile_done']:
                return
            text = f"Your request has been accepted. Your approved profile is **{safe(nickname(row))}** with **{row['kind'].title()}** access."
            if row['nickname_state'] == 'blocked':
                text += '\nThe nickname change could not be applied. Please ask staff to set it manually.'
        elif row['status'] == 'rejected':
            text = f"Your verification was rejected.\n\n**Reason:** {safe(row['reason'])}\n\nYou can reapply at {zurich_time(row['retry_after']) if row.get('retry_after') else 'any time'}."
        elif row['status'] == 'cancelled':
            text = 'Your pending request was removed by staff. You may submit a new request.'
        else:
            return
        try:
            user = await self.fetch_user(row['user_id'])
            message = embed('Application submitted' if field == 'submission_dm' else f"Verification {row['status']}", text)
            message.add_field(name='GAME USERNAME', value=safe(row['username']))
            message.add_field(name='VERIFICATION TYPE', value=row['kind'].title())
            message.color = 0x258568 if row['status'] == 'accepted' else 0x243B53
            message.set_footer(text=f"Application {row['id']} | SaS verification")
            await user.send(embed=message)
        except (discord.Forbidden, discord.NotFound):
            self.db.update(application_id, **{field: 'blocked'})
        else:
            self.db.update(application_id, **{field: 'done'})

    def history_embed(self, history):
        result = embed('Verification history', f"<@{history['user_id']}>\nConfirmed verification role transfers.")
        result.color = 0x258568
        entries = history['entries'][-8:]
        for entry in entries:
            result.add_field(name=zurich_time(entry['at']),
                value=f"**Username:** {safe(entry['username'], 200)}\n**Accepted by:** <@{entry['reviewer_id']}>\n**Roles:** Verify-Process removed; {entry.get('kind', 'member').title()} added.", inline=False)
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
        await self.attempt(self.apply_nickname, application_id)
        self.db.finish_profile(application_id)
        await self.attempt(self.send_dm, application_id, 'submission_dm')
        await self.attempt(self.send_dm, application_id, 'decision_dm')
        await self.attempt(self.sync_review, application_id)

    @tasks.loop(seconds=30)
    async def recover(self):
        async with self.lock:
            if self.db.paused()['dirty']:
                await self.attempt(self.ensure_panel)
            for job in self.db.nickname_jobs():
                if job['guild_id'] == self.guild.id:
                    await self.attempt(self.apply_member_prefix, job)
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
                self.db.queue_nickname(job['guild_id'], job['user_id'])
                self.db.finish_join(job['guild_id'], job['user_id'])

    async def discover_member_prefixes(self):
        try:
            async for member in self.guild.fetch_members(limit=None):
                if not member.bot and any(r.id == self.ids['MEMBER_ROLE_ID'] for r in member.roles):
                    async with self.lock:
                        self.db.queue_nickname(self.guild.id, member.id)
        except (discord.HTTPException, OSError):
            log.exception('Could not finish discovering Member nicknames; discovery will run again on restart')

    async def apply_member_prefix(self, job):
        if any(r['guild_id'] == self.guild.id and r['user_id'] == job['user_id'] and r['status'] == 'accepted' and not r['profile_done'] for r in self.db.applications()):
            return
        try:
            member = await self.guild.fetch_member(job['user_id'])
            if not any(r.id == self.ids['MEMBER_ROLE_ID'] for r in member.roles) or member.bot:
                self.db.finish_nickname_job(self.guild.id, job['user_id'], 'skipped')
                return
            profile = profile_from_member(member, self.ids, self.db.profile(self.guild.id, member.id))
            desired = nickname(profile)
            if member.nick != desired:
                await member.edit(nick=desired, reason='SaS Member nickname prefix')
            confirmed = await self.guild.fetch_member(member.id)
            if confirmed.nick != desired:
                return
            self.db.save_profile(self.guild.id, member.id, profile)
            self.db.finish_nickname_job(self.guild.id, member.id, 'done')
        except discord.NotFound:
            self.db.finish_nickname_job(self.guild.id, job['user_id'], 'skipped')
        except discord.HTTPException as error:
            if error.status not in (400, 403):
                raise
            self.db.finish_nickname_job(self.guild.id, job['user_id'], 'blocked')
            log.warning('Could not prefix Member %s; check nickname permissions and hierarchy, then set the nickname manually', job['user_id'])

    async def on_member_update(self, before, after):
        if not self.configured or after.bot or after.guild.id != self.guild.id:
            return
        member_role = self.ids['MEMBER_ROLE_ID']
        if member_role not in {r.id for r in before.roles} and member_role in {r.id for r in after.roles}:
            async with self.lock:
                self.db.queue_nickname(self.guild.id, after.id, force=True)

    async def on_member_join(self, member):
        if member.bot or not self.configured or member.guild.id != self.guild.id:
            return
        async with self.lock:
            self.db.queue_join(self.guild.id, member.id)
            try:
                await self.prepare_member(member)
                self.db.queue_nickname(self.guild.id, member.id, force=True)
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
        if self.prefix_task and not self.prefix_task.done():
            self.prefix_task.cancel()
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
