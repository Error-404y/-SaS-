"""Small, atomic SQLite operations. Run one bot instance per database."""
import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','accepted','rejected')),
                reviewer_id INTEGER, reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT, message_id INTEGER,
                roles_done INTEGER NOT NULL DEFAULT 0,
                review_done INTEGER NOT NULL DEFAULT 0,
                submission_dm TEXT NOT NULL DEFAULT 'pending',
                decision_dm TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_application
                ON applications(guild_id,user_id) WHERE status='pending';
        """)

    def create(self, guild_id, user_id, username):
        try:
            with self.conn:
                cursor = self.conn.execute(
                    'INSERT INTO applications(guild_id,user_id,username) VALUES(?,?,?)',
                    (guild_id, user_id, username))
            return self.get(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def get(self, application_id):
        return self.conn.execute('SELECT * FROM applications WHERE id=?', (application_id,)).fetchone()

    def by_message(self, message_id):
        return self.conn.execute('SELECT * FROM applications WHERE message_id=?', (message_id,)).fetchone()

    def update(self, application_id, **fields):
        allowed = {'message_id', 'roles_done', 'review_done', 'submission_dm', 'decision_dm'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError('Invalid database fields')
        with self.conn:
            self.conn.execute('UPDATE applications SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?',
                              (*fields.values(), application_id))

    def decide(self, application_id, reviewer_id, status, reason=None):
        if status not in ('accepted', 'rejected'):
            raise ValueError('Invalid decision')
        with self.conn:
            result = self.conn.execute("""UPDATE applications SET status=?,reviewer_id=?,reason=?,
                decided_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'""",
                (status, reviewer_id, reason, application_id))
        return result.rowcount == 1

    def work(self):
        return self.conn.execute("""SELECT * FROM applications WHERE message_id IS NULL
            OR submission_dm='pending' OR (status!='pending' AND
            (roles_done=0 OR review_done=0 OR decision_dm='pending'))""").fetchall()

    def setting(self, key):
        row = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, value))

    def close(self):
        self.conn.close()
