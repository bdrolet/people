import json
import os
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def _parse_port(value: str) -> int:
    # k8s injects POSTGRES_PORT as "tcp://host:port" for services named "postgres"
    if value.startswith("tcp://"):
        return int(value.rsplit(":", 1)[-1])
    return int(value)


def get_conn():
    connection_name = os.environ.get("CLOUD_SQL_CONNECTION_NAME")
    if connection_name:
        return _cloud_sql_conn(connection_name)
    return _direct_conn()


def _cloud_sql_conn(connection_name: str) -> "_Pg8000Conn":
    # connector v1.20 does not support 'psycopg' driver — use pg8000 instead.
    # pgvector.pg8000.register_vector expects pg8000.native.Connection, but the
    # connector returns pg8000.dbapi.Connection. Serialize vectors as strings in
    # _adapt_params instead — pgvector accepts the '[x,y,...]' text form implicitly.
    from google.cloud.sql.connector import Connector

    connector = Connector()
    pg_conn = connector.connect(
        connection_name,
        "pg8000",
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        db=os.environ.get("POSTGRES_DB", "people"),
    )
    return _Pg8000Conn(pg_conn)


def _direct_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=_parse_port(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "people"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        row_factory=dict_row,
    )


def _adapt_params(params: Optional[tuple]) -> Optional[list]:
    """Convert psycopg3-specific types to pg8000-compatible equivalents."""
    if params is None:
        return None
    result = []
    for p in params:
        if isinstance(p, Jsonb):
            result.append(json.dumps(p.obj))
        elif isinstance(p, list) and p and all(isinstance(x, (int, float)) for x in p):
            # pgvector: serialize float lists as '[x,y,...]' — accepted via implicit text cast
            result.append("[" + ",".join(str(x) for x in p) + "]")
        else:
            result.append(p)
    return result


class _DictCursor:
    """Wraps a pg8000 cursor to return dict rows, matching psycopg3 dict_row API."""

    def __init__(self, cursor: Any) -> None:
        self._c = cursor

    def _cols(self) -> list[str]:
        return [d[0] for d in self._c.description]

    def fetchone(self) -> Optional[dict]:
        row = self._c.fetchone()
        return dict(zip(self._cols(), row)) if row is not None else None

    def fetchall(self) -> list[dict]:
        cols = self._cols()
        return [dict(zip(cols, row)) for row in self._c.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._c.rowcount


class _Pg8000Conn:
    """Wraps pg8000 DBAPI2 connection with psycopg3-compatible execute() API."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, query: str, params: Optional[tuple] = None) -> _DictCursor:
        cursor = self._conn.cursor()
        adapted = _adapt_params(params)
        if adapted is None:
            cursor.execute(query)
        else:
            cursor.execute(query, adapted)
        return _DictCursor(cursor)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "_Pg8000Conn":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
