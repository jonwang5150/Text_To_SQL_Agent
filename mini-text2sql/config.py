"""Environment settings and the single LLM provider; importing has no side effects."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class Settings:
    model: str
    database_url: str = field(repr=False)
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_seconds: int = 120
    allowed_tables: tuple[str, ...] = ("public.air_quality_hourly",)
    statement_timeout_ms: int = 5000
    max_result_rows: int = 100


def load_settings() -> Settings:
    load_dotenv(Path(__file__).with_name(".env"))
    required = ("LLM_MODEL", "DATABASE_URL")
    if missing := [key for key in required if not os.getenv(key, "").strip()]:
        raise ValueError("缺少環境變數：" + ", ".join(missing))
    tables = tuple(dict.fromkeys(
        item.strip() for item in os.getenv(
            "ALLOWED_TABLES", "public.air_quality_hourly"
        ).split(",")
    ))
    # Configuration deliberately supports simple, lowercase, schema-qualified names.
    if not tables or any(
        not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", table)
        or table.split(".")[0] in {"pg_catalog", "information_schema"}
        or table.split(".")[0].startswith("pg_")
        for table in tables
    ):
        raise ValueError("ALLOWED_TABLES 必須是小寫 schema.table，以逗號分隔；不可指定系統 Schema。")
    try:
        url = make_url(os.environ["DATABASE_URL"].strip())
        if url.drivername != "postgresql+psycopg":
            raise ValueError
    except Exception:
        raise ValueError("DATABASE_URL 必須使用 postgresql+psycopg://。") from None
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
    try:
        parsed = urlsplit(ollama_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path or parsed.port == 0):
            raise ValueError
    except ValueError:
        raise ValueError("OLLAMA_BASE_URL 須為 http(s)://主機:連接埠，不含 /api、帳密或參數。") from None
    try:
        timeout = int(os.getenv("STATEMENT_TIMEOUT_MS", "5000"))
        rows = int(os.getenv("MAX_RESULT_ROWS", "100"))
        llm_timeout = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
        if not 1 <= timeout <= 60000 or not 1 <= rows <= 1000:
            raise ValueError
        if not 1 <= llm_timeout <= 600:
            raise ValueError
    except ValueError:
        raise ValueError("SQL 逾時須為 1～60000 ms，結果上限 1～1000 筆，LLM 逾時 1～600 秒。") from None
    return Settings(
        model=os.environ["LLM_MODEL"].strip(),
        database_url=os.environ["DATABASE_URL"].strip(),
        ollama_base_url=ollama_url,
        llm_timeout_seconds=llm_timeout,
        allowed_tables=tables,
        statement_timeout_ms=timeout,
        max_result_rows=rows,
    )


def create_llm(settings: Settings) -> ChatOllama:
    """Use one Ollama endpoint; cloud models use the local Ollama sign-in."""
    return ChatOllama(
        model=settings.model, base_url=settings.ollama_base_url,
        temperature=0, client_kwargs={"timeout": settings.llm_timeout_seconds},
    )
