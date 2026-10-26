import re
import unicodedata


def normalized_name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def clean_clan(value):
    value = ' '.join(value.split())
    return 'SaS' if normalized_name(value) == 'sas' else value


def valid_clan(value):
    return 1 <= len(value) <= 12 and any(c.isalnum() for c in value) and all(c.isalnum() or c in ' _-' for c in value)


def nickname(profile):
    clan = 'SaS' if profile['kind'] == 'member' else profile.get('clan', '')
    prefix = f'[{clan}] ' if clan else ''
    return prefix + untagged(profile['username'])[:max(0, 32 - len(prefix))]


def untagged(value):
    return re.sub(r'^(?:\[[^\]]+\]\s*)+', '', value).strip()


def profile_from_member(member, ids, saved=None):
    roles = {role.id for role in member.roles}
    if ids['MEMBER_ROLE_ID'] in roles:
        kind = 'member'
    elif ids['ALLIES_ROLE_ID'] in roles:
        kind = 'allies'
    else:
        return None
    display = member.nick or member.name
    tag = re.match(r'^\[([^\]]+)\]', display)
    clan = 'SaS' if kind == 'member' else (saved or {}).get('clan') or (tag.group(1) if tag and valid_clan(tag.group(1)) else '')
    return dict(kind=kind, clan=clan, username=untagged((saved or {}).get('username') or display) or member.name)
