"""Admin commands: `python -m app.cli create-user <name>` and `python -m app.cli gen-keys`.

The password is read with getpass (or from PLATFORM_PASSWORD for scripted setups); it is never
echoed, logged or passed on the command line.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys

from cryptography.fernet import Fernet
from sqlalchemy import delete, select

from .api.security import hash_password
from .audit import log as audit
from .core.config import get_settings
from .core.db import session_factory
from .core.models import User, UserSession
from .core.timeutil import utcnow


def create_user(username: str) -> int:
    password = os.environ.get("PLATFORM_PASSWORD")
    if not password:
        password = getpass.getpass("Парола (поне 12 знака): ")
        if password != getpass.getpass("Повтори паролата: "):
            print("Паролите не съвпадат.", file=sys.stderr)
            return 1
    try:
        digest = hash_password(password)
    except ValueError as exc:
        print(f"Отказано: {exc}", file=sys.stderr)
        return 1
    sf = session_factory(get_settings().database_url)
    with sf.begin() as session:
        user = session.scalar(select(User).where(User.username == username))
        if user is None:
            session.add(User(username=username, password_hash=digest, created_at=utcnow()))
            action = "user_created"
        else:
            user.password_hash = digest
            session.execute(delete(UserSession).where(UserSession.user_id == user.id))  # log out everywhere
            action = "user_password_reset"
        audit.record(session, "cli", action, None, {"username": username})
    print(f"Готово: {username}")
    return 0


def gen_keys() -> int:
    print("# Добави в .env (не ги комитвай):")
    print(f"SECRETS_MASTER_KEY={Fernet.generate_key().decode()}")
    print(f"MCP_TOKEN={secrets.token_urlsafe(32)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user", help="създай потребител или смени паролата му")
    cu.add_argument("username")
    sub.add_parser("gen-keys", help="генерирай SECRETS_MASTER_KEY и MCP_TOKEN")
    args = parser.parse_args(argv)
    if args.cmd == "create-user":
        return create_user(args.username)
    return gen_keys()


if __name__ == "__main__":
    sys.exit(main())
