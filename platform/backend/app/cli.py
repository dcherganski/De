"""Admin commands: `python -m app.cli create-user <name>`, `gen-keys`, `seed-demo`, `soak`, `evaluate`.

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


def seed_demo() -> int:
    import asyncio

    from .demo import seed_demo as run

    s = get_settings()
    print("Зареждам СИНТЕТИЧНИ демо данни (етикет fixture). Не са живи пазарни данни.")
    n = asyncio.run(run(session_factory(s.database_url), s))
    print(f"Готово: {n} синтетични събития.")
    return 0


def soak(hours: float, every_s: float, out: str) -> int:
    from pathlib import Path

    from .evaluation.soak import run

    s = get_settings()
    print(f"24-часов PAPER тест: {hours:g} ч, проба на всеки {every_s:g} с. Ctrl+C спира и записва частичен отчет.")
    report = run(session_factory(s.database_url), hours, every_s, Path(out))
    print(f"Отчет: {report}")
    return 0


def evaluate(tune_fraction: float, out: str | None) -> int:
    from .evaluation.historical import evaluate as run_eval
    from .evaluation.historical import load_rows, report_md

    s = get_settings()
    rows = load_rows(session_factory(s.database_url), s)
    if len(rows) < 10:
        print(f"Само {len(rows)} сигнала с резултат — твърде малко за тест. Остави PAPER работата да трупа данни.")
        return 1
    text = report_md(run_eval(rows, tune_fraction))
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user", help="създай потребител или смени паролата му")
    cu.add_argument("username")
    sub.add_parser("gen-keys", help="генерирай SECRETS_MASTER_KEY и MCP_TOKEN")
    sub.add_parser("seed-demo", help="СИНТЕТИЧНИ демо данни в празна, отделна база")
    sk = sub.add_parser("soak", help="24-часов PAPER тест с отчет (само чете базата)")
    sk.add_argument("--hours", type=float, default=24)
    sk.add_argument("--every", type=float, default=60, help="секунди между пробите")
    sk.add_argument("--out", default="reports")
    ev = sub.add_parser("evaluate", help="исторически тест на записаните сигнали без поглед напред")
    ev.add_argument("--tune-fraction", type=float, default=0.6)
    ev.add_argument("--out")
    args = parser.parse_args(argv)
    if args.cmd == "soak":
        return soak(args.hours, args.every, args.out)
    if args.cmd == "evaluate":
        return evaluate(args.tune_fraction, args.out)
    if args.cmd == "create-user":
        return create_user(args.username)
    if args.cmd == "seed-demo":
        return seed_demo()
    return gen_keys()


if __name__ == "__main__":
    sys.exit(main())
