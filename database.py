"""Local JSON storage for one running bot instance; no SQL or database server."""
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
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                raise ValueError(f'Invalid JSON data file: {self.path}. Restore a backup; the file has not been changed.') from error
        else:
            self._save({'version': 1, 'next_id': 1, 'settings': {}, 'applications': {}})

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('Unsupported storage format')
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
            if row['status'] not in ('pending', 'accepted', 'rejected'):
                raise ValueError('Invalid application status')

    def _save(self, data):
        # Write a separate file first so an interrupted write cannot truncate the
        # current data. Publish in-memory changes only after replacement succeeds.
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

    def create(self, guild_id, user_id, username):
        if any(row['guild_id'] == guild_id and row['user_id'] == user_id and row['status'] == 'pending'
               for row in self.data['applications'].values()):
            return None
        data = deepcopy(self.data)
        application_id = data['next_id']
        data['next_id'] += 1
        data['applications'][str(application_id)] = {
            'id': application_id, 'guild_id': guild_id, 'user_id': user_id,
            'username': username, 'status': 'pending', 'reviewer_id': None,
            'reason': None, 'created_at': timestamp(), 'decided_at': None,
            'message_id': None, 'roles_done': 0, 'review_done': 0,
            'submission_dm': 'pending', 'decision_dm': 'pending',
        }
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
        if status not in ('accepted', 'rejected'):
            raise ValueError('Invalid decision')
        row = self.get(application_id)
        if row is None or row['status'] != 'pending':
            return False
        data = deepcopy(self.data)
        data['applications'][str(application_id)].update(
            status=status, reviewer_id=reviewer_id, reason=reason, decided_at=timestamp())
        self._save(data)
        return True

    def work(self):
        return [deepcopy(row) for row in self.data['applications'].values()
                if row['message_id'] is None or row['submission_dm'] == 'pending'
                or (row['status'] != 'pending' and
                    (not row['roles_done'] or not row['review_done'] or row['decision_dm'] == 'pending'))]

    def setting(self, key):
        return self.data['settings'].get(key)

    def set_setting(self, key, value):
        data = deepcopy(self.data)
        data['settings'][key] = value
        self._save(data)

    def close(self):
        # Every change is saved immediately; no connection to close.
        pass
