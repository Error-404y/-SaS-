import math
import time


class EditDeferred(Exception):
    pass


class EditPacer:
    def __init__(self, database, clock=None):
        self._database = database
        self.clock = clock or time.time

    @property
    def database(self):
        return self._database() if callable(self._database) else self._database

    def ready(self, channel_id):
        return self.clock() >= max(self.state(channel_id)['next_at'], self.state('global')['next_at'])

    def state(self, channel_id):
        return self.database.edit_limit(str(channel_id)) or dict(next_at=0.0, spacing=6.0)

    def reserve(self, channel_id):
        now = self.clock()
        state = self.state(channel_id)
        if now < max(state['next_at'], self.state('global')['next_at']):
            raise EditDeferred()
        state['next_at'] = now + state['spacing']
        self.database.save_edit_limit(str(channel_id), state)

    def finished(self, channel_id):
        state = self.state(channel_id)
        state['next_at'] = max(state['next_at'], self.clock() + state['spacing'])
        self.database.save_edit_limit(str(channel_id), state)

    @staticmethod
    def seconds(value):
        try:
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def observe(self, channel_id, status, headers, body=None):
        state = self.state(channel_id)
        now = self.clock()
        reset_after = self.seconds(headers.get('X-RateLimit-Reset-After'))
        remaining = self.seconds(headers.get('X-RateLimit-Remaining'))
        delay = 0.0
        if reset_after:
            delay = reset_after / (remaining + 1) + 0.5
        if status == 429:
            body = body if isinstance(body, dict) else {}
            retry_after = max(self.seconds(headers.get('Retry-After')), self.seconds(body.get('retry_after')))
            state['spacing'] = min(120.0, max(6.0, state['spacing'] * 2))
            delay = max(delay, retry_after + 0.5, state['spacing'])
            if body.get('global') or headers.get('X-RateLimit-Global') == 'true':
                global_state = self.state('global')
                global_state['next_at'] = max(global_state['next_at'], now + delay)
                self.database.save_edit_limit('global', global_state)
        state['next_at'] = max(state['next_at'], now + delay)
        self.database.save_edit_limit(str(channel_id), state)
