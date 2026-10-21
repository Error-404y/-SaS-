from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


def timestamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                self._validate(self.data)
                self._upgrade()
                self._validate_extensions()
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                raise ValueError(f'Invalid JSON data file: {self.path}. Restore a backup; the file has not been changed.') from error
        else:
            self._save({'version': 1, 'next_id': 1, 'settings': {}, 'applications': {}, 'events': [], 'members': {}})

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('Unsupported storage format')
        if not isinstance(data.get('events', []), list) or not isinstance(data.get('members', {}), dict):
            raise ValueError('Invalid events or member state')
        if not isinstance(data['settings'], dict) or not isinstance(data['applications'], dict):
            raise ValueError('Invalid storage structure')
        if type(data['next_id']) is not int or data['next_id'] < 1:
            raise ValueError('Invalid next ID')
        required = {'id', 'guild_id', 'user_id', 'username', 'status', 'reviewer_id',
                    'reason', 'created_at', 'decided_at', 'message_id', 'roles_done',
                    'review_done', 'submission_dm', 'decision_dm'}
        for key, row in data['applications'].items():
            if not isinstance(row, dict) or not required <= row.keys():
                raise ValueError('Incomplete application')
            if type(row['id']) is not int or str(row['id']) != key or not 0 < row['id'] < data['next_id']:
                raise ValueError('Invalid application ID')
            if row['status'] not in ('pending', 'accepted', 'rejected', 'cancelled', 'revoked'):
                raise ValueError('Invalid application status')

    def _save(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self.path.parent, prefix='.verification-',
                                             suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.data = data

    def create(self, guild_id, user_id, username, language='en', answers=None, rules=None):
        if any(row['guild_id'] == guild_id and row['user_id'] == user_id and row['status'] == 'pending'
               for row in self.data['applications'].values()):
            return None
        latest = self.latest(guild_id, user_id)
        if latest and latest['status'] == 'accepted':
            raise ValueError('Already verified')
        if self.setting('paused'):
            raise ValueError('Applications are paused')
        limit = self.setting('queue_limit') or 0
        if limit and sum(r['status'] == 'pending' and r['guild_id'] == guild_id for r in self.data['applications'].values()) >= limit:
            raise ValueError('Application queue is full')
        data = deepcopy(self.data)
        application_id = data['next_id']
        data['next_id'] += 1
        data['applications'][str(application_id)] = {
            'id': application_id, 'guild_id': guild_id, 'user_id': user_id,
            'username': username, 'status': 'pending', 'reviewer_id': None,
            'reason': None, 'created_at': timestamp(), 'decided_at': None,
            'message_id': None, 'roles_done': 0, 'review_done': 0,
            'submission_dm': 'pending', 'decision_dm': 'pending',
            'language': language, 'answers': answers or {}, 'rules': rules,
            'revision': 0, 'claimed_by': None, 'question': None, 'answer': None,
            'note': None,
        }
        self._event(data, application_id, 'submitted', user_id)
        self._save(data)
        return self.get(application_id)

    def get(self, application_id):
        return deepcopy(self.data['applications'].get(str(application_id)))

    def by_message(self, message_id):
        return next((deepcopy(row) for row in self.data['applications'].values()
                     if row['message_id'] == message_id), None)

    def update(self, application_id, **fields):
        allowed = {'message_id', 'roles_done', 'review_done', 'submission_dm', 'decision_dm'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError('Invalid storage fields')
        data = deepcopy(self.data)
        data['applications'][str(application_id)].update(fields)
        self._save(data)

    def decide(self, application_id, reviewer_id, status, reason=None):
        row = self.get(application_id)
        if not row or row['status'] != 'pending':
            return False
        self.transition(application_id, reviewer_id, status, row['revision'], reason or '')
        return True

    def setting(self, key):
        return self.data['settings'].get(key)

    def set_setting(self, key, value):
        data = deepcopy(self.data)
        data['settings'][key] = value
        self._save(data)

    def close(self):
        pass

    def _upgrade(self):
        self.data.setdefault('events', [])
        self.data.setdefault('members', {})
        for row in self.data['applications'].values():
            defaults = dict(language='en', answers={}, rules=None, revision=0,
                            claimed_by=None, question=None, answer=None, note=None)
            for key, value in defaults.items():
                row.setdefault(key, value)
        for row in self.data['applications'].values():
            latest = self.latest(row['guild_id'], row['user_id'])
            if latest['id'] == row['id'] and row['status'] == 'accepted':
                key = f"{row['guild_id']}:{row['user_id']}"
                self.data['members'].setdefault(key, dict(guild_id=row['guild_id'], user_id=row['user_id'],
                    verified=True, dirty=not bool(row['roles_done'])))

        for row in self.data['applications'].values():
            if not any(e['application_id'] == row['id'] for e in self.data['events']):
                if row['submission_dm'] == 'pending':
                    self._event(self.data, row['id'], 'submitted', row['user_id'])
                if row['status'] in ('accepted', 'rejected') and row['decision_dm'] == 'pending':
                    self._event(self.data, row['id'], row['status'], row['reviewer_id'], row['reason'] or '')

    def latest(self, guild_id, user_id):
        rows = [r for r in self.data['applications'].values()
                if r['guild_id'] == guild_id and r['user_id'] == user_id]
        return deepcopy(max(rows, key=lambda r: r['id'])) if rows else None

    def duplicates(self, row):
        import unicodedata
        normalize = lambda value: unicodedata.normalize('NFKC', value).casefold().strip()
        return [r['user_id'] for r in self.data['applications'].values()
                if r['guild_id'] == row['guild_id'] and r['user_id'] != row['user_id']
                and r['status'] in ('accepted', 'pending')
                and normalize(r['username']) == normalize(row['username'])]

    def _event(self, data, application_id, action, actor, detail=''):
        data['events'].append(dict(id=len(data['events']) + 1, application_id=application_id,
            action=action, actor=actor, detail=detail, at=timestamp(), audit_done=False, dm_done=False,
            revision=data['applications'][str(application_id)]['revision'] if application_id else None))

    def transition(self, application_id, actor, action, revision, detail=''):
        data = deepcopy(self.data)
        row = data['applications'][str(application_id)]
        if row['revision'] != revision:
            raise ValueError('This application changed. Refresh and try again.')
        latest = self.latest(row['guild_id'], row['user_id'])
        if latest['id'] != application_id:
            raise ValueError('Only the latest application can be changed.')
        if action in ('cancelled', 'answer'):
            if actor != row['user_id']:
                raise ValueError('Only the applicant can do this.')
        elif actor == row['user_id']:
            raise ValueError('You cannot review your own application.')
        if action in ('claim', 'release', 'question', 'accepted', 'rejected', 'cancelled', 'answer'):
            if row['status'] != 'pending':
                raise ValueError('This application is no longer pending.')
        if action in ('question', 'accepted', 'rejected', 'release') and row['claimed_by'] not in (None, actor):
            raise ValueError('Another staff member has claimed this application.')
        if action == 'claim':
            if row['claimed_by'] not in (None, actor):
                raise ValueError('Another staff member has claimed this application.')
            row['claimed_by'] = actor
        elif action == 'release':
            row['claimed_by'] = None
        elif action == 'question':
            if not detail.strip():
                raise ValueError('A question is required.')
            row.update(question=detail, answer=None, claimed_by=actor)
        elif action == 'answer':
            if not row['question'] or row['answer'] is not None or not detail.strip():
                raise ValueError('There is no unanswered question or the answer is empty.')
            row['answer'] = detail
        elif action in ('accepted', 'rejected', 'cancelled', 'revoked', 'undo'):
            if action in ('rejected', 'revoked', 'undo') and not detail.strip():
                raise ValueError('A reason is required.')
            if action == 'revoked' and row['status'] != 'accepted':
                raise ValueError('Only accepted verification can be revoked.')
            if action == 'undo' and row['status'] not in ('accepted', 'rejected'):
                raise ValueError('Only an acceptance or rejection can be reopened.')
            if action == 'undo':
                limit = data['settings'].get('queue_limit', 0)
                count = sum(r['status'] == 'pending' and r['guild_id'] == row['guild_id'] for r in data['applications'].values())
                if limit and count >= limit:
                    raise ValueError('Queue full. Increase the limit before reopening.')
            row.update(status='pending' if action == 'undo' else action,
                       reviewer_id=actor, decided_at=timestamp(), reason=detail,
                       note=detail if action == 'accepted' else None,
                       roles_done=0, decision_dm='pending')
            if action == 'undo':
                row.update(claimed_by=None, question=None, answer=None)
            key = f"{row['guild_id']}:{row['user_id']}"
            data['members'][key] = dict(guild_id=row['guild_id'], user_id=row['user_id'],
                verified=action == 'accepted', dirty=True)
        else:
            raise ValueError('Unknown action')
        row['revision'] += 1
        row['review_done'] = 0
        self._event(data, application_id, action, actor, detail)
        self._save(data)
        return self.get(application_id)

    def events(self):
        return deepcopy(self.data['events'])

    def event_done(self, event_id, field):
        if field not in ('dm_done', 'audit_done'):
            raise ValueError('Invalid event field')
        data = deepcopy(self.data)
        event = data['events'][event_id - 1]
        event[field] = True
        if field == 'dm_done' and event['application_id']:
            row = data['applications'][str(event['application_id'])]
            if event['action'] == 'submitted':
                row['submission_dm'] = 'complete'
            elif event['action'] in ('accepted', 'rejected', 'cancelled', 'revoked', 'undo') and event['revision'] == row['revision']:
                row['decision_dm'] = 'complete'
        self._save(data)

    def members(self):
        return deepcopy(list(self.data['members'].values()))

    def member_state(self, guild_id, user_id):
        return deepcopy(self.data['members'].get(f'{guild_id}:{user_id}'))

    def track_member(self, guild_id, user_id, verified=False):
        data = deepcopy(self.data)
        key = f'{guild_id}:{user_id}'
        state = data['members'].setdefault(key, dict(guild_id=guild_id, user_id=user_id, verified=verified))
        state['dirty'] = True
        self._save(data)

    def roles_applied(self, guild_id, user_id):
        data = deepcopy(self.data)
        data['members'][f'{guild_id}:{user_id}']['dirty'] = False
        row = self.latest(guild_id, user_id)
        if row:
            data['applications'][str(row['id'])]['roles_done'] = 1
            data['applications'][str(row['id'])]['review_done'] = 0
            self._event(data, row['id'], 'roles', 0,
                        'Member granted; Verify-Progress removed' if data['members'][f'{guild_id}:{user_id}']['verified']
                        else 'Verify-Progress granted; Member removed')
        self._save(data)

    def configure(self, actor, paused, reason, queue_limit):
        if queue_limit < 0 or queue_limit > 1000:
            raise ValueError('Queue limit must be between 0 and 1000.')
        if paused and not reason.strip():
            raise ValueError('Provide an explanation when pausing applications.')
        data = deepcopy(self.data)
        data['settings'].update(paused=paused, pause_reason=reason, queue_limit=queue_limit)
        self._event(data, None, 'configuration', actor, f'Paused: {paused}; limit: {queue_limit}; {reason}')
        self._save(data)

    def _validate_extensions(self):
        if not isinstance(self.data['events'], list) or not isinstance(self.data['members'], dict):
            raise ValueError('Invalid events or member state')
        for index, event in enumerate(self.data['events'], 1):
            if not isinstance(event, dict) or event['id'] != index:
                raise ValueError('Invalid event ID')
            for key in ('action', 'detail', 'at'):
                if not isinstance(event[key], str):
                    raise ValueError('Invalid event text')
            if event['application_id'] is not None and str(event['application_id']) not in self.data['applications']:
                raise ValueError('Event application missing')
            for key in ('dm_done', 'audit_done'):
                if type(event[key]) is not bool:
                    raise ValueError('Invalid delivery state')
        for key, state in self.data['members'].items():
            if not isinstance(state, dict) or key != f"{state['guild_id']}:{state['user_id']}":
                raise ValueError('Invalid member state')
            if type(state['verified']) is not bool or type(state['dirty']) is not bool:
                raise ValueError('Invalid role state')
        for row in self.data['applications'].values():
            if row['language'] not in ('en', 'de') or type(row['revision']) is not int or row['revision'] < 0:
                raise ValueError('Invalid application preferences')
            if not isinstance(row['answers'], dict) or not isinstance(row['username'], str):
                raise ValueError('Invalid application answers')
