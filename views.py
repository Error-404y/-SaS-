import logging
import unicodedata

import discord

from language import tr

log = logging.getLogger('verification')


def embed(title, description=''):
    return discord.Embed(title=title[:256], description=description[:4096], color=discord.Color.blurple())


async def reply(interaction, title, description='', view=None):
    kwargs = dict(embed=embed(title, description), ephemeral=True, view=view)
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


def valid_username(value):
    return (1 <= len(value) <= 100 and any(c.isalnum() for c in value)
            and not any(unicodedata.category(c).startswith('C') for c in value)
            and not any(s in value.casefold() for s in ('http:', 'https:', 'www.', '<@', '@everyone', '@here')))


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        log.error('View action failed', exc_info=(type(error), error, error.__traceback__))
        await reply(interaction, 'Unable to complete action', 'Please try again or contact staff.')


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        log.error('Modal failed', exc_info=(type(error), error, error.__traceback__))
        await reply(interaction, 'Unable to complete action', 'Please try again or contact staff.')


class ApplicationModal(SafeModal):
    def __init__(self, bot, language, rules):
        super().__init__(title=tr(language, 'title'), timeout=300)
        self.bot, self.language, self.rules = bot, language, rules
        self.username = discord.ui.TextInput(label=tr(language, 'username'), max_length=100)
        self.patrol = discord.ui.TextInput(label=tr(language, 'patrol'), max_length=300)
        self.experience = discord.ui.TextInput(label=tr(language, 'experience'), style=discord.TextStyle.paragraph, max_length=600)
        for item in (self.username, self.patrol, self.experience):
            self.add_item(item)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = str(self.username).strip()
        if not valid_username(name):
            await reply(interaction, tr(self.language, 'error'), tr(self.language, 'invalid'))
            return
        if not str(self.patrol).strip() or not str(self.experience).strip():
            await reply(interaction, tr(self.language, 'error'), tr(self.language, 'retry'))
            return
        async with self.bot.lock:
            if not await self.bot.applicant_allowed(interaction, self.language):
                return
            try:
                row = self.bot.db.create(interaction.guild_id, interaction.user.id, name, self.language,
                    {'patrol': str(self.patrol).strip(), 'experience': str(self.experience).strip()}, self.rules)
            except ValueError as error:
                await reply(interaction, tr(self.language, 'error'), str(error))
                return
            if row is None:
                await reply(interaction, tr(self.language, 'error'), tr(self.language, 'duplicate'))
                return
            await self.bot.try_process(row['id'])
        await reply(interaction, tr(self.language, 'saved'), tr(self.language, 'submitted') + '\n' + tr(self.language, 'closed_dm'))


class RulesView(SafeView):
    def __init__(self, bot, language, user_id):
        super().__init__(timeout=300)
        self.bot, self.language, self.user_id = bot, language, user_id
        self.rules = bot.rules(language)
        button = discord.ui.Button(label=tr(language, 'agree'), style=discord.ButtonStyle.success)
        button.callback = self.agree
        self.add_item(button)

    async def agree(self, interaction):
        if interaction.user.id != self.user_id or not self.bot.public_context(interaction):
            return await reply(interaction, 'Not allowed')
        await interaction.response.send_modal(ApplicationModal(self.bot, self.language, self.rules))


class VerifyView(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Verify', style=discord.ButtonStyle.success, custom_id='sas:verify:v1')
    async def verify(self, interaction, button):
        await self.start(interaction, 'en')

    @discord.ui.button(label='Deutsch', custom_id='sas:verify:de')
    async def german(self, interaction, button):
        await self.start(interaction, 'de')

    async def start(self, interaction, language):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if await self.bot.applicant_allowed(interaction, language):
            view = RulesView(self.bot, language, interaction.user.id)
            await reply(interaction, tr(language, 'title'), tr(language, 'rules') + '\n\n' + view.rules, view)

    @discord.ui.button(label='My application / Mein Antrag', custom_id='sas:status:v2')
    async def status(self, interaction, button):
        if not self.bot.public_context(interaction):
            return await reply(interaction, 'Not allowed')
        row = self.bot.db.latest(interaction.guild_id, interaction.user.id)
        language = row['language'] if row else ('de' if str(interaction.locale).startswith('de') else 'en')
        if not row:
            return await reply(interaction, tr(language, 'status'), tr(language, 'none'))
        view = ApplicantView(self.bot, row) if row['status'] == 'pending' else None
        await interaction.response.send_message(embed=self.bot.applicant_embed(row), view=view, ephemeral=True)


class ApplicantView(SafeView):
    def __init__(self, bot, row):
        super().__init__(timeout=300)
        self.bot, self.row = bot, row
        cancel = discord.ui.Button(label=tr(row['language'], 'cancel'), style=discord.ButtonStyle.danger)
        cancel.callback = self.cancel
        self.add_item(cancel)
        if row['question'] and row['answer'] is None:
            answer = discord.ui.Button(label=tr(row['language'], 'respond'))
            answer.callback = self.answer
            self.add_item(answer)

    async def cancel(self, interaction):
        await self.bot.apply_action(interaction, self.row, 'cancelled', applicant=True)

    async def answer(self, interaction):
        if interaction.user.id != self.row['user_id'] or not self.bot.public_context(interaction):
            return await reply(interaction, 'Not allowed')
        await interaction.response.send_modal(ActionModal(self.bot, self.row, 'answer', applicant=True))


class ActionModal(SafeModal):
    def __init__(self, bot, row, action, default='', applicant=False):
        language = row['language'] if applicant else 'en'
        labels = {'accepted': 'Optional acceptance note', 'rejected': 'Reason for rejection',
                  'question': 'Question for the applicant', 'answer': tr(language, 'reply'),
                  'undo': 'Reason for undoing the decision', 'revoked': 'Reason for revoking verification'}
        super().__init__(title=labels[action][:45], timeout=300)
        self.bot, self.row, self.action, self.applicant = bot, row, action, applicant
        self.value = discord.ui.TextInput(label=labels[action], style=discord.TextStyle.paragraph,
                                         max_length=800, required=action != 'accepted', default=default)
        self.add_item(self.value)

    async def on_submit(self, interaction):
        await self.bot.apply_action(interaction, self.row, self.action, str(self.value).strip(), self.applicant)


class TemplateView(SafeView):
    def __init__(self, bot, row):
        super().__init__(timeout=300)
        self.bot, self.row = bot, row

    @discord.ui.select(placeholder='Choose a reason to customize', options=[
        discord.SelectOption(label='Custom reason', value='custom'),
        discord.SelectOption(label='Username needs correction', value='username'),
        discord.SelectOption(label='More detail required', value='detail'),
        discord.SelectOption(label='Rules not met', value='rules')])
    async def template(self, interaction, select):
        if not self.bot.staff_context(interaction):
            return await reply(interaction, 'Not allowed')
        reasons = {
            'en': {'custom': '', 'username': 'Please check and correct your in-game username.',
                   'detail': 'Please provide more detail about your experience and availability.',
                   'rules': 'Your application does not meet the server rules. Please review them before applying again.'},
            'de': {'custom': '', 'username': 'Bitte pruefe und korrigiere deinen Benutzernamen im Spiel.',
                   'detail': 'Bitte gib mehr Informationen zu deiner Erfahrung und Verfuegbarkeit an.',
                   'rules': 'Dein Antrag entspricht nicht den Serverregeln. Bitte lies sie vor einem neuen Antrag.'},
        }
        await interaction.response.send_modal(ActionModal(self.bot, self.row, 'rejected', reasons[self.row['language']][select.values[0]]))


class ReviewView(SafeView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def row(self, interaction):
        if not self.bot.staff_context(interaction):
            await reply(interaction, 'Not allowed', 'Only High-Rank staff may use this in verify-check.')
            return None
        row = self.bot.db.by_message(interaction.message.id)
        if not row or row['user_id'] == interaction.user.id:
            await reply(interaction, 'Not allowed', 'Application missing or self-review attempted.')
            return None
        return row

    @discord.ui.button(label='Accept', style=discord.ButtonStyle.success, custom_id='sas:accept:v1')
    async def accept(self, interaction, button):
        row = await self.row(interaction)
        if row:
            await interaction.response.send_modal(ActionModal(self.bot, row, 'accepted'))

    @discord.ui.button(label='Reject', style=discord.ButtonStyle.danger, custom_id='sas:reject:v1')
    async def reject(self, interaction, button):
        row = await self.row(interaction)
        if row:
            await reply(interaction, 'Rejection reason', 'Choose a template or write your own reason.', TemplateView(self.bot, row))

    @discord.ui.button(label='Claim / Release', custom_id='sas:claim:v2')
    async def claim(self, interaction, button):
        row = await self.row(interaction)
        if row:
            await self.bot.apply_action(interaction, row, 'release' if row['claimed_by'] == interaction.user.id else 'claim')

    @discord.ui.button(label='Request information', custom_id='sas:question:v2')
    async def question(self, interaction, button):
        row = await self.row(interaction)
        if row:
            await interaction.response.send_modal(ActionModal(self.bot, row, 'question'))

    @discord.ui.button(label='Undo decision', custom_id='sas:undo:v2', row=1)
    async def undo(self, interaction, button):
        row = await self.row(interaction)
        if row:
            await interaction.response.send_modal(ActionModal(self.bot, row, 'undo'))

    @discord.ui.button(label='Revoke verification', style=discord.ButtonStyle.danger, custom_id='sas:revoke:v2', row=1)
    async def revoke(self, interaction, button):
        row = await self.row(interaction)
        if row:
            await interaction.response.send_modal(ActionModal(self.bot, row, 'revoked'))
