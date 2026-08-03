"""
Database layer for the AI Meeting Manager.

Runs on plain SQLite for local testing, and automatically switches to
Postgres in production by looking for a DATABASE_URL environment variable
(hosting platforms like Render set this for you automatically once you
attach a database). No other code needs to change between the two.

The one rule that matters most in this file either way: every meetings/
action_items query is scoped by user_id (or joined back to a meeting owned
by that user_id). That's what keeps one office's data invisible to another.
"""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = "meeting_manager.db"
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Some hosts hand out URLs starting with postgres:// - psycopg2 wants postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_db():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _q(query):
    """All queries in this file are written with '?' placeholders (sqlite
    style). Postgres wants '%s' instead - swap them in only when needed."""
    return query.replace("?", "%s") if USE_POSTGRES else query


def _fetchone(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(_q(query), params)
    return cur.fetchone()


def _fetchall(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(_q(query), params)
    return cur.fetchall()


def _insert_and_get_id(conn, query, params):
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(_q(query) + " RETURNING id", params)
        new_id = cur.fetchone()["id"]
    else:
        cur.execute(query, params)
        new_id = cur.lastrowid
    conn.commit()
    return new_id


def _execute(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(_q(query), params)
    conn.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = get_db()
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'en',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meetings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                meeting_date TEXT NOT NULL,
                attendees TEXT,
                transcript TEXT,
                summary TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_items (
                id SERIAL PRIMARY KEY,
                meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                description TEXT NOT NULL,
                owner TEXT,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_meetings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                attendees TEXT,
                status TEXT NOT NULL DEFAULT 'scheduled',
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_documents (
                id SERIAL PRIMARY KEY,
                scheduled_meeting_id INTEGER NOT NULL REFERENCES scheduled_meetings(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content_type TEXT,
                content_b64 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                scheduled_meeting_id INTEGER REFERENCES scheduled_meetings(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            """
        )
    else:
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'en',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                meeting_date TEXT NOT NULL,
                attendees TEXT,
                transcript TEXT,
                summary TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                owner TEXT,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS scheduled_meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                attendees TEXT,
                status TEXT NOT NULL DEFAULT 'scheduled',
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS scheduled_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheduled_meeting_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT,
                content_b64 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                FOREIGN KEY (scheduled_meeting_id) REFERENCES scheduled_meetings(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                scheduled_meeting_id INTEGER,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (scheduled_meeting_id) REFERENCES scheduled_meetings(id) ON DELETE CASCADE
            );
            """
        )
    conn.commit()

    # Defensive migration: if this database was created before the
    # `language` column existed, add it now instead of crashing.
    try:
        cur.execute(_q("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'en'"))
        conn.commit()
    except Exception:
        conn.rollback()

    conn.close()


# ---------- users ----------

def create_user(name, email, password_hash, language="en"):
    conn = get_db()
    user_id = _insert_and_get_id(
        conn,
        "INSERT INTO users (name, email, password_hash, language, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, email, password_hash, language, now_iso()),
    )
    conn.close()
    return user_id


def update_user_language(user_id, language):
    conn = get_db()
    _execute(conn, "UPDATE users SET language = ? WHERE id = ?", (language, user_id))
    conn.close()


def get_user_by_email(email):
    conn = get_db()
    row = _fetchone(conn, "SELECT * FROM users WHERE email = ?", (email,))
    conn.close()
    return row


def get_user_by_id(user_id):
    conn = get_db()
    row = _fetchone(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
    conn.close()
    return row


# ---------- meetings (always scoped to a user_id) ----------

def create_meeting(user_id, title, meeting_date, attendees, transcript, summary):
    conn = get_db()
    meeting_id = _insert_and_get_id(
        conn,
        """INSERT INTO meetings (user_id, title, meeting_date, attendees, transcript, summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, title, meeting_date, attendees, transcript, summary, now_iso()),
    )
    conn.close()
    return meeting_id


def list_meetings_for_user(user_id):
    conn = get_db()
    rows = _fetchall(
        conn,
        "SELECT * FROM meetings WHERE user_id = ? ORDER BY meeting_date DESC, id DESC",
        (user_id,),
    )
    conn.close()
    return rows


def list_meetings_with_stats_for_user(user_id):
    """Same as list_meetings_for_user, but also includes how many action
    items each meeting has and how many are done - used on the dashboard
    so each row can show real progress instead of just a title."""
    conn = get_db()
    rows = _fetchall(
        conn,
        """
        SELECT m.*,
               COUNT(a.id) AS total_items,
               COALESCE(SUM(CASE WHEN a.done = 1 THEN 1 ELSE 0 END), 0) AS done_items
        FROM meetings m
        LEFT JOIN action_items a ON a.meeting_id = m.id
        WHERE m.user_id = ?
        GROUP BY m.id
        ORDER BY m.meeting_date DESC, m.id DESC
        """,
        (user_id,),
    )
    conn.close()
    return rows


def get_meeting_for_user(meeting_id, user_id):
    """Returns the meeting ONLY if it belongs to this user, else None.
    This is the check that prevents one person from viewing another
    person's meeting by guessing/changing the URL id."""
    conn = get_db()
    row = _fetchone(conn, "SELECT * FROM meetings WHERE id = ? AND user_id = ?", (meeting_id, user_id))
    conn.close()
    return row


# ---------- action items (always reached through a meeting the user owns) ----------

def add_action_item(meeting_id, description, owner):
    conn = get_db()
    _execute(
        conn,
        "INSERT INTO action_items (meeting_id, description, owner, done, created_at) VALUES (?, ?, ?, 0, ?)",
        (meeting_id, description, owner, now_iso()),
    )
    conn.close()


def list_action_items_for_meeting(meeting_id):
    conn = get_db()
    rows = _fetchall(conn, "SELECT * FROM action_items WHERE meeting_id = ? ORDER BY id", (meeting_id,))
    conn.close()
    return rows


def toggle_action_item(action_item_id, meeting_id):
    """meeting_id is required too, so a toggle call can't touch an action
    item that belongs to a different meeting/owner."""
    conn = get_db()
    _execute(
        conn,
        "UPDATE action_items SET done = 1 - done WHERE id = ? AND meeting_id = ?",
        (action_item_id, meeting_id),
    )
    conn.close()

# ---------- scheduled meetings (planned in advance, confirmed closer to the time) ----------

def create_scheduled_meeting(user_id, title, scheduled_at, attendees):
    conn = get_db()
    scheduled_id = _insert_and_get_id(
        conn,
        "INSERT INTO scheduled_meetings (user_id, title, scheduled_at, attendees, status, reminder_sent, created_at) "
        "VALUES (?, ?, ?, ?, 'scheduled', 0, ?)",
        (user_id, title, scheduled_at, attendees, now_iso()),
    )
    conn.close()
    return scheduled_id


def list_upcoming_scheduled_meetings_for_user(user_id):
    """Everything not yet cancelled, soonest first - used on the dashboard
    and the Upcoming page."""
    conn = get_db()
    rows = _fetchall(
        conn,
        "SELECT * FROM scheduled_meetings WHERE user_id = ? AND status != 'cancelled' ORDER BY scheduled_at ASC",
        (user_id,),
    )
    conn.close()
    return rows


def get_scheduled_meeting_for_user(scheduled_id, user_id):
    conn = get_db()
    row = _fetchone(
        conn, "SELECT * FROM scheduled_meetings WHERE id = ? AND user_id = ?", (scheduled_id, user_id)
    )
    conn.close()
    return row


def set_scheduled_meeting_status(scheduled_id, status):
    conn = get_db()
    _execute(conn, "UPDATE scheduled_meetings SET status = ? WHERE id = ?", (status, scheduled_id))
    conn.close()


def find_due_reminders(window_end_iso, now_iso_value):
    """System-wide (not scoped to one user) - finds every scheduled
    meeting, for any account, that starts within the reminder window and
    hasn't had a reminder sent yet. This is what the periodic check calls."""
    conn = get_db()
    rows = _fetchall(
        conn,
        "SELECT * FROM scheduled_meetings WHERE status = 'scheduled' AND reminder_sent = 0 "
        "AND scheduled_at >= ? AND scheduled_at <= ?",
        (now_iso_value, window_end_iso),
    )
    conn.close()
    return rows


def mark_reminder_sent(scheduled_id):
    conn = get_db()
    _execute(conn, "UPDATE scheduled_meetings SET reminder_sent = 1 WHERE id = ?", (scheduled_id,))
    conn.close()


# ---------- documents attached to a scheduled meeting ----------

def add_scheduled_document(scheduled_meeting_id, filename, content_type, content_b64):
    conn = get_db()
    _execute(
        conn,
        "INSERT INTO scheduled_documents (scheduled_meeting_id, filename, content_type, content_b64, uploaded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scheduled_meeting_id, filename, content_type, content_b64, now_iso()),
    )
    conn.close()


def list_documents_for_scheduled_meeting(scheduled_meeting_id):
    conn = get_db()
    rows = _fetchall(
        conn,
        "SELECT id, scheduled_meeting_id, filename, content_type, uploaded_at FROM scheduled_documents "
        "WHERE scheduled_meeting_id = ? ORDER BY id",
        (scheduled_meeting_id,),
    )
    conn.close()
    return rows


def get_document_for_user(document_id, user_id):
    """Joins back to scheduled_meetings to confirm this document's meeting
    actually belongs to the requesting user before returning its content."""
    conn = get_db()
    row = _fetchone(
        conn,
        """
        SELECT d.* FROM scheduled_documents d
        JOIN scheduled_meetings m ON m.id = d.scheduled_meeting_id
        WHERE d.id = ? AND m.user_id = ?
        """,
        (document_id, user_id),
    )
    conn.close()
    return row


# ---------- notifications (the "is this still happening?" prompts) ----------

def create_notification(user_id, scheduled_meeting_id, message):
    conn = get_db()
    _execute(
        conn,
        "INSERT INTO notifications (user_id, scheduled_meeting_id, message, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (user_id, scheduled_meeting_id, message, now_iso()),
    )
    conn.close()


def list_pending_notifications_for_user(user_id):
    conn = get_db()
    rows = _fetchall(
        conn,
        "SELECT * FROM notifications WHERE user_id = ? AND status = 'pending' ORDER BY id",
        (user_id,),
    )
    conn.close()
    return rows


def get_notification_for_user(notification_id, user_id):
    conn = get_db()
    row = _fetchone(
        conn, "SELECT * FROM notifications WHERE id = ? AND user_id = ?", (notification_id, user_id)
    )
    conn.close()
    return row


def mark_notification_answered(notification_id):
    conn = get_db()
    _execute(conn, "UPDATE notifications SET status = 'answered' WHERE id = ?", (notification_id,))
    conn.close()
