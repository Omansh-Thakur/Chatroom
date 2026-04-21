import sqlite3
import threading

DB_NAME = "chat.db"
DB_LOCK = threading.Lock()
TEXT_MESSAGE_TYPE = "text"
IMAGE_MESSAGE_TYPE = "image"
AUDIO_MESSAGE_TYPE = "audio"

def init_db():
    with DB_LOCK:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT,
            message TEXT,
            message_type TEXT NOT NULL DEFAULT 'text',
            image_data TEXT,
            audio_path TEXT,
            audio_size INTEGER,
            original_size INTEGER,
            processed_size INTEGER,
            reduction_percent REAL
        )
        """)

        cursor.execute("PRAGMA table_info(messages)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if "message_type" not in existing_columns:
            cursor.execute(
                "ALTER TABLE messages ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'"
            )

        if "image_data" not in existing_columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN image_data TEXT")

        if "audio_path" not in existing_columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN audio_path TEXT")

        if "audio_size" not in existing_columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN audio_size INTEGER")

        if "original_size" not in existing_columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN original_size INTEGER")

        if "processed_size" not in existing_columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN processed_size INTEGER")

        if "reduction_percent" not in existing_columns:
            cursor.execute("ALTER TABLE messages ADD COLUMN reduction_percent REAL")

        conn.commit()
        conn.close()

def save_message(
    nickname,
    message,
    message_type=TEXT_MESSAGE_TYPE,
    image_data=None,
    audio_path=None,
    audio_size=None,
    original_size=None,
    processed_size=None,
    reduction_percent=None,
):
    with DB_LOCK:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO messages (
                nickname,
                message,
                message_type,
                image_data,
                audio_path,
                audio_size,
                original_size,
                processed_size,
                reduction_percent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                nickname,
                message,
                message_type,
                image_data,
                audio_path,
                audio_size,
                original_size,
                processed_size,
                reduction_percent,
            )
        )

        conn.commit()
        message_id = cursor.lastrowid
        conn.close()

    return message_id

def get_last_messages(limit=50):
    with DB_LOCK:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                nickname,
                CASE
                    WHEN COALESCE(message_type, 'text') = 'image' THEN
                        CASE
                            WHEN TRIM(COALESCE(message, '')) <> '' THEN '[Image] ' || message
                            ELSE '[Image]'
                        END
                    WHEN COALESCE(message_type, 'text') = 'audio' THEN
                        CASE
                            WHEN TRIM(COALESCE(message, '')) <> '' THEN '[Audio] ' || message
                            ELSE '[Audio]'
                        END
                    ELSE COALESCE(message, '')
                END AS rendered_message
            FROM messages
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )

        rows = cursor.fetchall()
        conn.close()

    return rows[::-1]  # oldest -> newest

def get_last_messages_with_ids(limit=50):
    with DB_LOCK:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id,
                nickname,
                COALESCE(message, ''),
                COALESCE(message_type, 'text'),
                image_data,
                audio_path,
                audio_size,
                original_size,
                processed_size,
                reduction_percent
            FROM messages
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )

        rows = cursor.fetchall()
        conn.close()

    return rows[::-1]  # oldest -> newest

def get_messages_after(last_id, limit=200):
    with DB_LOCK:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                id,
                nickname,
                COALESCE(message, ''),
                COALESCE(message_type, 'text'),
                image_data,
                audio_path,
                audio_size,
                original_size,
                processed_size,
                reduction_percent
            FROM messages
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (last_id, limit)
        )

        rows = cursor.fetchall()
        conn.close()

    return rows
