from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from identity import normalized_name


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def user_key(guild_id, user_id):
    return f'{guild_id}:{user_id}'


class Database:
    def __init__(self, path, cooldown_seconds=600):
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save(dict(version=2, next_id=1, panel_message=None, applications={}, verified={}, histories={}, joins={}, profiles={}, pause={'active': False, 'reason': '', 'dirty': False, 'control_dirty': False}, nickname_jobs={}, control_panel=None))
            return
        try:
            original = self.path.read_text(encoding='utf-8')
            data = json.loads(original)
            migrated = data.get('version') == 1
            if migrated:
                data = self._migrate(data)
            for row in data['applications'].values():
                row.setdefault('kind', 'member')
                row.setdefault('nickname_state', 'legacy' if row['status'] == 'accepted' and row['roles_done'] else 'pending')
                row.setdefault('retry_after', None)
                row.setdefault('operation', 'verification')
                row.setdefault('clan', 'SaS' if row['kind'] == 'member' else '')
                row.setdefault('previous', None)
                row.setdefault('profile_done', row['status'] == 'accepted' and row['roles_done'] and row['nickname_state'] != 'pending')
            data.setdefault('profiles', {})
            data.setdefault('pause', dict(active=False, reason='', dirty=False))
            data.setdefault('nickname_jobs', {})
            data.setdefault('control_panel', None)
            data['pause'].setdefault('control_dirty', False)
            for row in sorted(data['applications'].values(), key=lambda r: r['id'], reverse=True):
                key = user_key(row['guild_id'], row['user_id'])
                if row['status'] == 'accepted' and row['profile_done'] and key not in data['profiles']:
                    data['profiles'][key] = dict(username=row['username'], clan=row['clan'], kind=row['kind'])
            self._validate(data)
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError) as error:
            raise ValueError(f'Invalid data in {self.path}; the file was not changed.') from error
        self.data = data
        if migrated:
            backup = self.path.with_name(self.path.name + '.v1.bak')
            if not backup.exists():
                with backup.open('x', encoding='utf-8') as handle:
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
            self._save(data)

    @staticmethod
    def _migrate(old):
        data = dict(version=2, next_id=old['next_id'], panel_message=old['settings'].get('panel_message'),
                    applications={}, verified={}, histories={}, joins={})
        for key, source in old['applications'].items():
            deleted = source['message_id'] is None
            status = 'cancelled' if deleted and source['status'] == 'pending' else source['status']
            row = dict(id=source['id'], guild_id=source['guild_id'], user_id=source['user_id'],
                       username=source['username'], status=status, created_at=source['created_at'],
                       decided_at=source['decided_at'], reviewer_id=source['reviewer_id'], reason=source['reason'] or '',
                       message_id=source['message_id'], review_deleted=deleted, review_dirty=not deleted,
                       roles_done=bool(source['roles_done']), transfer_started=False, transfer_eligible=False,
                       submission_dm='done', decision_dm='done')
            if status == 'accepted' and not row['roles_done']:
                row['decision_dm'] = 'pending'
            data['applications'][key] = row
        latest = {}
        for row in data['applications'].values():
            key = user_key(row['guild_id'], row['user_id'])
            if key not in latest or row['id'] > latest[key]['id']:
                latest[key] = row
        for key, row in latest.items():
            if row['status'] == 'accepted':
                data['verified'][key] = True
        return data

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict) or data['version'] != 2 or type(data['next_id']) is not int or data['next_id'] < 1:
            raise ValueError('Invalid data version or sequence')
        for key in ('applications', 'verified', 'histories', 'joins', 'profiles', 'pause', 'nickname_jobs'):
            if not isinstance(data[key], dict):
                raise ValueError('Invalid data structure')
        if type(data['pause'].get('active')) is not bool or type(data['pause'].get('dirty')) is not bool or not isinstance(data['pause'].get('reason'), str):
            raise ValueError('Invalid pause settings')
        for key, profile in data['profiles'].items():
            guild_id, user_id = map(int, key.split(':'))
            if guild_id <= 0 or user_id <= 0 or profile['kind'] not in ('member', 'allies') or not isinstance(profile['username'], str) or not isinstance(profile['clan'], str):
                raise ValueError('Invalid saved profile')
        for job in data['nickname_jobs'].values():
            if job['state'] not in ('pending', 'done', 'blocked', 'skipped'):
                raise ValueError('Invalid nickname job')
        required = {'id', 'guild_id', 'user_id', 'username', 'status', 'created_at', 'decided_at',
                    'reviewer_id', 'reason', 'message_id', 'review_deleted', 'review_dirty', 'roles_done',
                    'transfer_started', 'transfer_eligible', 'submission_dm', 'decision_dm'}
        for key, row in data['applications'].items():
            if not isinstance(row, dict) or not required <= row.keys():
                raise ValueError('Incomplete application')
            if type(row['id']) is not int or str(row['id']) != key or not 0 < row['id'] < data['next_id']:
                raise ValueError('Invalid application ID')
            if row['operation'] not in ('verification', 'name', 'clan', 'join_member') or not isinstance(row['clan'], str) or type(row['profile_done']) is not bool:
                raise ValueError('Invalid profile request')
            if row['kind'] not in ('member', 'allies') or row['nickname_state'] not in ('pending', 'done', 'blocked', 'legacy'):
                raise ValueError('Invalid verification type or nickname state')
            if row['retry_after'] is not None:
                if datetime.fromisoformat(row['retry_after']).tzinfo is None:
                    raise ValueError('Cooldown needs a timezone')
            if row['status'] not in ('pending', 'accepted', 'rejected', 'cancelled', 'revoked'):
                raise ValueError('Invalid application status')
            if not isinstance(row['username'], str) or not isinstance(row['reason'], str):
                raise ValueError('Invalid application text')
            for field in ('review_deleted', 'review_dirty', 'roles_done', 'transfer_started', 'transfer_eligible'):
                if type(row[field]) is not bool:
                    raise ValueError('Invalid application state')
            for field in ('submission_dm', 'decision_dm'):
                if row[field] not in ('pending', 'done', 'blocked'):
                    raise ValueError('Invalid delivery state')
        for history in data['histories'].values():
            if not isinstance(history['entries'], list) or type(history['dirty']) is not bool or type(history['deleted']) is not bool:
                raise ValueError('Invalid verification history')
            for entry in history['entries']:
                if str(entry['application_id']) not in data['applications']:
                    raise ValueError('Unknown history application')
                datetime.fromisoformat(entry['at'])

    def _save(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.verification-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.data = data

    def applications(self):
        return deepcopy(list(self.data['applications'].values()))

    def get(self, application_id):
        return deepcopy(self.data['applications'].get(str(application_id)))

    def pending(self, guild_id, user_id):
        return next((row for row in self.applications() if row['guild_id'] == guild_id
                     and row['user_id'] == user_id and row['status'] == 'pending'), None)

    def by_message(self, message_id):
        return next((row for row in self.applications() if row['message_id'] == message_id), None)

    def is_verified(self, guild_id, user_id):
        return self.data['verified'].get(user_key(guild_id, user_id), False)

    def profile(self, guild_id, user_id):
        return deepcopy(self.data['profiles'].get(user_key(guild_id, user_id)))

    def save_profile(self, guild_id, user_id, profile):
        data = deepcopy(self.data)
        data['profiles'][user_key(guild_id, user_id)] = deepcopy(profile)
        self._save(data)

    def verified_kind(self, guild_id, user_id):
        profile = self.profile(guild_id, user_id)
        if profile:
            return profile['kind']
        rows = [r for r in self.applications() if r['guild_id'] == guild_id and r['user_id'] == user_id and r['status'] == 'accepted']
        return max(rows, key=lambda r: r['id'])['kind'] if rows else 'member'

    def busy(self, guild_id, user_id):
        return self.pending(guild_id, user_id) or next((r for r in self.applications()
            if r['guild_id'] == guild_id and r['user_id'] == user_id
            and r['status'] == 'accepted' and not r['profile_done']), None)

    def cooldown_until(self, guild_id, user_id):
        dates = [datetime.fromisoformat(r['retry_after']) for r in self.applications()
                 if r['guild_id'] == guild_id and r['user_id'] == user_id and r.get('retry_after')]
        until = max(dates) if dates else None
        return until if until and until > datetime.now(timezone.utc) else None

    def duplicates(self, row):
        matches = set()
        for key, profile in self.data['profiles'].items():
            guild_id, user_id = map(int, key.split(':'))
            if guild_id == row['guild_id'] and user_id != row['user_id'] and normalized_name(profile['username']) == normalized_name(row['username']):
                matches.add(user_id)
        for accepted in self.applications():
            key = user_key(accepted['guild_id'], accepted['user_id'])
            if key not in self.data['profiles'] and accepted['guild_id'] == row['guild_id'] and accepted['user_id'] != row['user_id'] and accepted['status'] == 'accepted' and accepted['roles_done'] and normalized_name(accepted['username']) == normalized_name(row['username']):
                matches.add(accepted['user_id'])
        return sorted(matches)

    def create(self, guild_id, user_id, username, kind='member', clan='', operation='verification', previous=None):
        if operation not in ('verification', 'name', 'clan', 'join_member'):
            raise ValueError('Invalid request type.')
        if operation != 'verification' and not previous:
            raise ValueError('Verified profile required.')
        if self.paused()['active'] and operation in ('verification', 'join_member'):
            raise ValueError('Applications are paused.')
        if kind not in ('member', 'allies'):
            raise ValueError('Choose Member or Allies.')
        if self.cooldown_until(guild_id, user_id):
            raise ValueError('Please wait until your rejection cooldown ends before applying again.')
        if self.busy(guild_id, user_id):
            return None
        if operation == 'verification' and self.is_verified(guild_id, user_id):
            raise ValueError('You are already verified.')
        data = deepcopy(self.data)
        application_id = data['next_id']
        data['next_id'] += 1
        data['applications'][str(application_id)] = dict(
            id=application_id, guild_id=guild_id, user_id=user_id, username=username,
            kind=kind, clan='SaS' if kind == 'member' else clan, operation=operation, previous=deepcopy(previous),
            profile_done=False, nickname_state='pending', retry_after=None,
            status='pending', created_at=timestamp(), decided_at=None, reviewer_id=None, reason='',
            message_id=None, review_deleted=False, review_dirty=True, roles_done=False,
            transfer_started=False, transfer_eligible=False, submission_dm='pending', decision_dm='done')
        self._save(data)
        return self.get(application_id)

    def update(self, application_id, **fields):
        allowed = {'nickname_state', 'message_id', 'review_dirty', 'transfer_started', 'transfer_eligible', 'submission_dm', 'decision_dm'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError('Invalid update fields')
        data = deepcopy(self.data)
        row = data['applications'][str(application_id)]
        row.update(fields)
        if fields.keys() & {'submission_dm', 'decision_dm'} and not row['review_deleted']:
            row['review_dirty'] = True
        self._save(data)

    def decide(self, application_id, reviewer_id, status, reason=''):
        if status not in ('accepted', 'rejected') or (status == 'rejected' and not reason.strip()):
            raise ValueError('A rejection requires a reason.')
        row = self.get(application_id)
        if not row or row['status'] != 'pending' or row['review_deleted']:
            return False
        data = deepcopy(self.data)
        data['applications'][str(application_id)].update(status=status, reviewer_id=reviewer_id,
            decided_at=timestamp(), reason=reason.strip(), review_dirty=True, decision_dm='pending',
            roles_done=status == 'rejected')
        if status == 'rejected':
            data['applications'][str(application_id)]['retry_after'] = (datetime.now(timezone.utc) + timedelta(seconds=self.cooldown_seconds)).isoformat()
        if status == 'accepted':
            data['verified'][user_key(row['guild_id'], row['user_id'])] = True
        self._save(data)
        return True

    def delete_review(self, application_id):
        data = deepcopy(self.data)
        row = data['applications'][str(application_id)]
        row.update(review_deleted=True, review_dirty=False)
        if row['status'] == 'pending':
            row.update(status='cancelled', decided_at=timestamp(), decision_dm='pending', submission_dm='done',
                       reason='The pending request was removed by staff. You may submit again.')
        self._save(data)

    def finish_acceptance(self, application_id):
        data = deepcopy(self.data)
        row = data['applications'][str(application_id)]
        if row['roles_done'] or row['status'] != 'accepted':
            return
        row.update(roles_done=True, review_dirty=not row['review_deleted'])
        for pending in data['applications'].values():
            if pending['guild_id'] == row['guild_id'] and pending['status'] == 'pending' and not pending['review_deleted']:
                pending['review_dirty'] = True
        if row['transfer_eligible']:
            key = user_key(row['guild_id'], row['user_id'])
            history = data['histories'].setdefault(key, dict(guild_id=row['guild_id'], user_id=row['user_id'],
                message_id=None, deleted=False, dirty=False, entries=[]))
            if not any(e['application_id'] == application_id for e in history['entries']):
                history['entries'].append(dict(application_id=application_id, username=row['username'],
                    reviewer_id=row['reviewer_id'], kind=row['kind'], at=timestamp()))
                history['dirty'] = not history['deleted']
        self._save(data)

    def finish_profile(self, application_id):
        data = deepcopy(self.data)
        row = data['applications'][str(application_id)]
        if row['status'] != 'accepted' or not row['roles_done'] or row['nickname_state'] == 'pending' or row['profile_done']:
            return
        data['profiles'][user_key(row['guild_id'], row['user_id'])] = dict(username=row['username'], clan=row['clan'], kind=row['kind'])
        row['profile_done'] = True
        for pending in data['applications'].values():
            if pending['guild_id'] == row['guild_id'] and pending['status'] == 'pending' and not pending['review_deleted']:
                pending['review_dirty'] = True
        self._save(data)

    def paused(self):
        return deepcopy(self.data['pause'])

    def set_pause(self, active, reason, actor_id):
        data = deepcopy(self.data)
        data['pause'] = dict(active=active, reason=reason.strip() if active else '', actor_id=actor_id, at=timestamp(), dirty=True, control_dirty=True)
        self._save(data)

    def panel_synced(self, control=False):
        data = deepcopy(self.data)
        data['pause']['control_dirty' if control else 'dirty'] = False
        self._save(data)

    def control_panel(self, channel_id=None, message_id=None):
        if message_id is not None:
            data = deepcopy(self.data)
            data['control_panel'] = dict(channel_id=channel_id, message_id=message_id)
            self._save(data)
        return deepcopy(self.data.get('control_panel'))

    def queue_nickname(self, guild_id, user_id, force=False):
        data = deepcopy(self.data)
        key = user_key(guild_id, user_id)
        if key in data['nickname_jobs'] and not force:
            return
        data['nickname_jobs'][key] = dict(guild_id=guild_id, user_id=user_id, state='pending')
        self._save(data)

    def nickname_jobs(self):
        return deepcopy([job for job in self.data['nickname_jobs'].values() if job['state'] == 'pending'])

    def finish_nickname_job(self, guild_id, user_id, state):
        data = deepcopy(self.data)
        data['nickname_jobs'][user_key(guild_id, user_id)]['state'] = state
        self._save(data)

    def histories(self):
        return deepcopy(list(self.data['histories'].values()))

    def update_history(self, guild_id, user_id, **fields):
        if not fields or not fields.keys() <= {'message_id', 'dirty', 'deleted'}:
            raise ValueError('Invalid history fields')
        data = deepcopy(self.data)
        data['histories'][user_key(guild_id, user_id)].update(fields)
        self._save(data)

    def queue_join(self, guild_id, user_id):
        data = deepcopy(self.data)
        data['joins'][user_key(guild_id, user_id)] = dict(guild_id=guild_id, user_id=user_id)
        self._save(data)

    def finish_join(self, guild_id, user_id):
        data = deepcopy(self.data)
        data['joins'].pop(user_key(guild_id, user_id), None)
        self._save(data)

    def joins(self):
        return deepcopy(list(self.data['joins'].values()))

    def panel(self, message_id=None):
        if message_id is not None:
            data = deepcopy(self.data)
            data['panel_message'] = message_id
            self._save(data)
        return self.data['panel_message']
