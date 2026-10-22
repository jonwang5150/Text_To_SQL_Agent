"""Environment settings and the single LLM provider; importing has no side effects."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from sqlalchemy.engine import make_url


# 集中保存不可變的模型、資料庫與查詢限制設定。
@dataclass(frozen=True)
class Settings:
    model: str
    database_url: str = field(repr=False)
    allowed_tables: tuple[str, ...]
    ollama_base_url: str = "https://ollama.com"
    llm_timeout_seconds: int = 120
    statement_timeout_ms: int = 5000
    max_result_rows: int = 100
    ollama_api_key: str | None = field(default=None, repr=False)


# 載入並驗證環境設定，避免以無效或不安全的參數啟動。
def load_settings() -> Settings:
    load_dotenv(Path(__file__).with_name(".env"))
    required = ("LLM_MODEL", "DATABASE_URL", "ALLOWED_TABLES")
    if missing := [key for key in required if not os.getenv(key, "").strip()]:
        raise ValueError("缺少環境變數：" + ", ".join(missing))
    tables = tuple(dict.fromkeys(
        item.strip() for item in os.environ["ALLOWED_TABLES"].split(",")
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
    ollama_url = os.getenv("OLLAMA_BASE_URL", "https://ollama.com").strip().rstrip("/")
    try:
        parsed = urlsplit(ollama_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path or parsed.port == 0):
            raise ValueError
    except ValueError:
        raise ValueError("OLLAMA_BASE_URL 須為 http(s)://主機:連接埠，不含 /api、帳密或參數。") from None
    api_key = os.getenv("OLLAMA_API_KEY", "").strip() or None
    if parsed.hostname == "ollama.com":
        if parsed.scheme != "https":
            raise ValueError("Ollama Cloud 的 OLLAMA_BASE_URL 必須使用 https://ollama.com。")
        if not api_key:
            raise ValueError("缺少環境變數：OLLAMA_API_KEY；直接連線 Ollama Cloud 需要 API Key。")
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
        ollama_api_key=api_key,
        llm_timeout_seconds=llm_timeout,
        allowed_tables=tables,
        statement_timeout_ms=timeout,
        max_result_rows=rows,
    )


# 依設定建立 Ollama 模型用戶端，套用驗證資訊與逾時限制。
def create_llm(settings: Settings) -> ChatOllama:
    """Connect directly to Ollama Cloud with bearer authentication."""
    client_kwargs = {"timeout": settings.llm_timeout_seconds}
    if settings.ollama_api_key:
        client_kwargs["headers"] = {"Authorization": f"Bearer {settings.ollama_api_key}"}
    return ChatOllama(
        model=settings.model, base_url=settings.ollama_base_url,
        temperature=0, client_kwargs=client_kwargs,
    )
