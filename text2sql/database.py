"""PostgreSQL metadata and bounded execution, always in read-only transactions."""

from contextlib import contextmanager
from typing import Any, Iterator, TypedDict

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from config import Settings


class QueryResult(TypedDict):
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool


class DatabaseError(RuntimeError):
    def __init__(self, message: str, kind: str = "database"):
        super().__init__(message)
        self.kind = kind


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url, connect_args={"connect_timeout": 5},
        pool_pre_ping=True, hide_parameters=True,
    )


def database_error(exc: SQLAlchemyError) -> DatabaseError:
    """Expose useful SQL diagnostics, never SQLAlchemy's URL/statement traceback."""
    original = getattr(exc, "orig", None)
    code = getattr(original, "sqlstate", None)
    if code == "42501":
        return DatabaseError("資料庫權限不足，請檢查唯讀帳號設定。", "permission")
    if not code or code.startswith(("08", "28", "53", "57P")):
        return DatabaseError("資料庫無法連線或服務不可用，請檢查服務與連線設定。", "connection")
    if code == "57014":
        return DatabaseError("SQL 查詢逾時，請縮小查詢範圍。", "query")
    if code.startswith(("42", "22")):
        # These object names are useful for repair; raw error text can contain data.
        diag = getattr(original, "diag", None)
        column = getattr(diag, "column_name", None)
        table = getattr(diag, "table_name", None)
        descriptions = {
            "42703": "欄位不存在，請依 Schema 核對欄位",
            "42P01": "資料表不存在，請依 Schema 核對資料表",
            "42601": "SQL 語法錯誤", "42702": "欄位名稱不明確，請加上資料表別名",
            "42883": "函式或運算子不符合參數型別", "42803": "聚合與 GROUP BY 不一致",
        }
        message = descriptions.get(code, "SQL 語法或資料型別錯誤")
        detail = f"；column={column}" if column else ""
        detail += f"；table={table}" if table else ""
        return DatabaseError(f"PostgreSQL {code}：{message}{detail}", "query")
    return DatabaseError(f"資料庫執行失敗（SQLSTATE {code}）。")


@contextmanager
def readonly_connection(engine: Engine, timeout_ms: int) -> Iterator[Connection]:
    """Apply protections locally to this transaction, including metadata reads."""
    with engine.connect() as conn, conn.begin():
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.execute(text("SELECT pg_catalog.set_config('statement_timeout', :v, true)"),
                     {"v": str(timeout_ms)})
        conn.execute(text("SELECT pg_catalog.set_config('lock_timeout', :v, true)"),
                     {"v": str(min(timeout_ms, 1000))})
        # User tables are fully qualified by the validator. Avoid function shadowing.
        conn.exec_driver_sql("SET LOCAL search_path = pg_catalog")
        yield conn


def load_schema(engine: Engine, settings: Settings) -> str:
    """Called once at startup; only whitelist metadata enters the prompt cache."""
    try:
        with readonly_connection(engine, settings.statement_timeout_ms) as conn:
            powerful = conn.execute(text("""
                SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication
                       OR rolbypassrls
                FROM pg_catalog.pg_roles WHERE rolname = current_user
            """)).scalar_one()
            mutable = conn.execute(text("""
                SELECT pg_catalog.has_database_privilege(current_database(), 'CREATE')
                    OR pg_catalog.has_database_privilege(current_database(), 'TEMP')
            """)).scalar_one()
            if powerful or mutable:
                raise DatabaseError("帳號權限過大：請使用無管理、CREATE 與 TEMP 權限的唯讀帳號。", "permission")
            lines = []
            for qualified in settings.allowed_tables:
                schema, table = qualified.split(".")
                columns = conn.execute(text("""
                    SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod)
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
                    WHERE n.nspname = :schema AND c.relname = :table
                      AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
                    ORDER BY a.attnum
                """), {"schema": schema, "table": table}).all()
                if not columns:
                    raise DatabaseError(f"找不到白名單資料表，或並非一般／分割表：{qualified}", "schema")
                rights = conn.execute(text("""
                    SELECT pg_catalog.has_table_privilege(:qualified, 'SELECT') AS can_read,
                           pg_catalog.has_table_privilege(
                               :qualified, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                           ) AS can_write,
                           pg_catalog.has_schema_privilege(:schema, 'CREATE') AS can_create
                """), {"qualified": qualified, "schema": schema}).mappings().one()
                if not rights["can_read"] or rights["can_write"] or rights["can_create"]:
                    raise DatabaseError(f"資料表權限不符合唯讀要求：{qualified}", "permission")
                lines.append(f"Table: {qualified}\n" + "\n".join(
                    f"  {name}: {datatype}" for name, datatype in columns
                ))
            return "\n\n".join(lines)
    except SQLAlchemyError as exc:
        raise database_error(exc) from None


def execute_sql(engine: Engine, sql: str, settings: Settings) -> QueryResult:
    """Accept only validator output; the graph is the authorized entry point."""
    try:
        with readonly_connection(engine, settings.statement_timeout_ms) as conn:
            # No DBAPI interpolation: SQL literals may legitimately contain '%' (LIKE/modulo).
            with conn.execution_options(stream_results=True, no_parameters=True).exec_driver_sql(sql) as result:
                columns = list(result.keys())
                rows = result.fetchmany(settings.max_result_rows + 1)
                return {
                    "columns": columns,
                    "rows": [tuple(row) for row in rows[:settings.max_result_rows]],
                    "truncated": len(rows) > settings.max_result_rows,
                }
    except SQLAlchemyError as exc:
        raise database_error(exc) from None
