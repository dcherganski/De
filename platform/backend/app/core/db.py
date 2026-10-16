"""Database engine and session factory (SQLAlchemy 2, psycopg 3, PostgreSQL)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import Numeric, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# 38 digits with 18 after the point holds satoshis, wei-scale fees and EUR amounts exactly.
Num = Numeric(38, 18, asdecimal=True)


class Base(DeclarativeBase):
    type_annotation_map = {Decimal: Num}


_engines: dict[str, Engine] = {}


def get_engine(url: str) -> Engine:
    if url not in _engines:
        _engines[url] = create_engine(url, pool_pre_ping=True, future=True)
    return _engines[url]


def session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
