"""تسجيل حركات النظام (إضافة / تعديل / حذف) — للمدير فقط."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from flask import session


def ensure_activity_log_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Activity_Log (
                Log_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Action_Type TEXT NOT NULL,
                Entity_Type TEXT NOT NULL,
                Ref_No TEXT,
                Title TEXT,
                Details TEXT,
                Username TEXT,
                Created_At TEXT DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_activity_ref ON Activity_Log(Ref_No)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_activity_title ON Activity_Log(Title)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_activity_created ON Activity_Log(Created_At)'
        )
        conn.commit()
    finally:
        conn.close()


def log_activity(
    db_path: str,
    action_type: str,
    entity_type: str,
    *,
    ref_no: Any = '',
    title: Any = '',
    details: Any = '',
    username: Optional[str] = None,
) -> None:
    """يسجّل حركة دون إفساد العملية الأساسية عند الفشل."""
    try:
        user = username if username is not None else (session.get('username') or '')
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute(
                '''INSERT INTO Activity_Log
                   (Action_Type, Entity_Type, Ref_No, Title, Details, Username)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (
                    str(action_type or '').strip() or '—',
                    str(entity_type or '').strip() or '—',
                    str(ref_no or '').strip(),
                    str(title or '').strip(),
                    str(details or '').strip(),
                    str(user or '').strip(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def search_activity(
    db_path: str,
    *,
    q: str = '',
    action: str = '',
    limit: int = 500,
) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        sql = 'SELECT * FROM Activity_Log WHERE 1=1'
        params: list[Any] = []
        q = (q or '').strip()
        if q:
            like = f'%{q}%'
            sql += ' AND (Ref_No LIKE ? OR Title LIKE ? OR Details LIKE ?)'
            params.extend([like, like, like])
        action = (action or '').strip()
        if action:
            sql += ' AND Action_Type = ?'
            params.append(action)
        sql += ' ORDER BY Log_ID DESC LIMIT ?'
        params.append(int(limit))
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()
