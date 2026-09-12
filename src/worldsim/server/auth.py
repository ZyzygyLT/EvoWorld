"""Accounts: username+password (pbkdf2 stdlib), token sessions.

Anonymous play keeps working (random tokens, no account). Logging in binds
your worlds and claims to a real account: 'Mis mundos' travels with you.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from sqlalchemy import select

from worldsim.server import db
from worldsim.server.db import SessionRow, UserRow

_ROUNDS = 200_000


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ROUNDS)
    return f"{salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    candidate = _hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, stored)


def register(username: str, password: str) -> dict | str:
    username = username.strip()
    if not (3 <= len(username) <= 24):
        return "username must be 3-24 characters"
    if len(password) < 4:
        return "password must be at least 4 characters"
    with db.SessionLocal() as s:
        exists = s.scalars(
            select(UserRow).where(UserRow.username == username)
        ).first()
        if exists is not None:
            return "username is taken"
        user = UserRow(
            username=username,
            pw_hash=_hash_password(password),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        token = secrets.token_hex(24)
        s.add(SessionRow(token=token, user_id=user.id,
                         created_at=time.strftime("%Y-%m-%d %H:%M:%S")))
        s.commit()
        return {"token": token, "username": username, "user_id": user.id}


def login(username: str, password: str) -> dict | str:
    with db.SessionLocal() as s:
        user = s.scalars(
            select(UserRow).where(UserRow.username == username.strip())
        ).first()
        if user is None or not _check_password(password, user.pw_hash):
            return "invalid username or password"
        token = secrets.token_hex(24)
        s.add(SessionRow(token=token, user_id=user.id,
                         created_at=time.strftime("%Y-%m-%d %H:%M:%S")))
        s.commit()
        return {"token": token, "username": user.username, "user_id": user.id}


def logout(token: str) -> None:
    with db.SessionLocal() as s:
        row = s.get(SessionRow, token)
        if row is not None:
            s.delete(row)
            s.commit()


def user_from_token(token: str | None) -> UserRow | None:
    if not token:
        return None
    with db.SessionLocal() as s:
        row = s.get(SessionRow, token)
        if row is None:
            return None
        return s.get(UserRow, row.user_id)


__all__ = ["register", "login", "logout", "user_from_token"]
