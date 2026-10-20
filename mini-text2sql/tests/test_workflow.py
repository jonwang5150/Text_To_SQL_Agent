"""Real graph/validator tests; no API key, network, or PostgreSQL required."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
import json

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlglot import parse_one

import config
import database
from config import Settings
from database import DatabaseError, database_error, execute_sql, load_schema, readonly_connection
from graph import GRAPH_RECURSION_LIMIT, build_graph, initial_state
from nodes import format_answer, validate_sql


TABLES = ("public.air_quality_hourly",)
SCHEMA = "Table: public.air_quality_hourly\n  observed_at: timestamp\n  aqi: integer"
GOOD_SQL = "SELECT MAX(aqi) AS max_aqi FROM public.air_quality_hourly"
RESULT = {"columns": ["max_aqi"], "rows": [(123,)], "truncated": False}


def run_workflow(responses, results=None, *, startup_error=None, question="哪一天 AQI 最大？"):
    llm = Mock()
    llm.invoke.side_effect = [
        item if isinstance(item, Exception) else AIMessage(content=item) for item in responses
    ]
    execute = Mock()
    execute.side_effect = results if results is not None else [RESULT]
    workflow = build_graph(llm, SCHEMA, TABLES, execute, startup_error=startup_error)
    state = workflow.invoke(initial_state(question),
                            config={"recursion_limit": GRAPH_RECURSION_LIMIT})
    return state, llm, execute


def test_success():
    state, llm, execute = run_workflow([GOOD_SQL])
    assert state["status"] == "success"
    assert state["retry_count"] == 0
    assert "123" in state["final_answer"]
    assert state["error_message"] is None
    assert llm.invoke.call_count == execute.call_count == 1
    assert "LIMIT 101" in state["sql_query"]


def test_syntax_error_repaired_before_execution():
    state, llm, execute = run_workflow(["SELECT (", GOOD_SQL])
    assert state["status"] == "success"
    assert state["retry_count"] == 1
    assert execute.call_count == 1
    repair_payload = llm.invoke.call_args_list[1].args[0][1][1]
    assert '"sql": "SELECT ("' in repair_payload
    assert '"error":' in repair_payload and '"schema":' in repair_payload


@pytest.mark.parametrize("message", ["PostgreSQL 42601：SQL 語法錯誤", "PostgreSQL 42703：欄位不存在"])
def test_database_sql_error_repaired(message):
    bad = "SELECT missing_column FROM public.air_quality_hourly"
    state, llm, execute = run_workflow([bad, GOOD_SQL], [DatabaseError(message, "query"), RESULT])
    assert state["status"] == "success" and state["retry_count"] == 1
    assert execute.call_count == llm.invoke.call_count == 2
    assert state["error_message"] is None
    assert message in llm.invoke.call_args_list[1].args[0][1][1]


def test_validation_stops_after_exactly_three_repairs():
    state, llm, execute = run_workflow(["DELETE FROM public.air_quality_hourly"] * 4)
    assert state["status"] == "failed" and state["retry_count"] == 3
    assert llm.invoke.call_count == 4
    execute.assert_not_called()
    assert "已修正 3 次" in state["final_answer"]
    assert state["error_message"]


def test_execution_stops_after_exactly_three_repairs():
    state, llm, execute = run_workflow([GOOD_SQL] * 4,
        [DatabaseError("SQL 執行失敗", "query") for _ in range(4)])
    assert state["status"] == "failed" and state["retry_count"] == 3
    assert llm.invoke.call_count == execute.call_count == 4


def test_third_repair_can_succeed():
    state, llm, execute = run_workflow(["SELECT ("] * 3 + [GOOD_SQL])
    assert state["status"] == "success" and state["retry_count"] == 3
    assert llm.invoke.call_count == 4 and execute.call_count == 1


def test_empty_result_is_success():
    state, _, _ = run_workflow([GOOD_SQL], [{"columns": ["aqi"], "rows": [], "truncated": False}])
    assert state["status"] == "success"
    assert state["final_answer"] == "查無符合條件的資料。"
    assert state["retry_count"] == 0


@pytest.mark.parametrize("kind", ["connection", "permission", "database"])
def test_unrepairable_database_failure(kind):
    state, llm, execute = run_workflow([GOOD_SQL], [DatabaseError("資料庫無法連線", kind)])
    assert state["status"] == "failed" and state["retry_count"] == 0
    assert llm.invoke.call_count == execute.call_count == 1


def test_schema_startup_failure_does_not_call_llm_or_database():
    state, llm, execute = run_workflow([], startup_error="資料庫無法連線")
    assert state["status"] == "failed"
    assert "資料庫無法連線" in state["final_answer"]
    llm.invoke.assert_not_called()
    execute.assert_not_called()


def test_blank_question():
    state, llm, execute = run_workflow([], question="   ")
    assert "請輸入" in state["final_answer"]
    llm.invoke.assert_not_called()
    execute.assert_not_called()


def test_llm_failure_is_sanitized():
    state, llm, execute = run_workflow([RuntimeError("secret-api-key")])
    assert state["status"] == "failed" and state["retry_count"] == 0
    assert "secret-api-key" not in state["final_answer"]
    assert llm.invoke.call_count == 1
    execute.assert_not_called()


def test_llm_failure_during_repair():
    state, llm, execute = run_workflow(["SELECT (", RuntimeError("secret")])
    assert state["status"] == "failed" and state["retry_count"] == 1
    assert llm.invoke.call_count == 2
    execute.assert_not_called()


@pytest.mark.parametrize("sql", [
    "DELETE FROM public.air_quality_hourly",
    "UPDATE public.air_quality_hourly SET aqi = 0",
    "INSERT INTO public.air_quality_hourly (aqi) VALUES (1)",
    "DROP TABLE public.air_quality_hourly",
    "ALTER TABLE public.air_quality_hourly ADD COLUMN x int",
    "TRUNCATE public.air_quality_hourly",
    "CREATE TABLE x (id int)",
    "SELECT 1; SELECT 2",
    "SELECT 1; DELETE FROM public.air_quality_hourly",
    "WITH x AS (DELETE FROM public.air_quality_hourly RETURNING *) SELECT * FROM x",
    "SELECT * INTO temp_x FROM public.air_quality_hourly",
    "SELECT * FROM public.air_quality_hourly FOR UPDATE",
    "SELECT * FROM secret.payroll",
    "SELECT * FROM pg_catalog.pg_authid",
    "WITH x AS (SELECT * FROM secret.payroll) SELECT * FROM x",
    "WITH air_quality_hourly AS (SELECT * FROM secret.payroll) SELECT * FROM air_quality_hourly",
    "SELECT * FROM public.air_quality_hourly WHERE EXISTS (SELECT 1 FROM secret.payroll)",
    "SELECT pg_sleep(1)",
    "SELECT nextval('some_sequence')",
    "SELECT set_config('search_path', 'public', false)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_advisory_lock(1)",
    "SELECT public.max(aqi) FROM public.air_quality_hourly",
    "SELECT dblink_exec('host=example', 'DELETE FROM t')",
    "SELECT * FROM generate_series(1, 1000000)",
    "SELECT CAST(aqi AS public.custom_type) FROM public.air_quality_hourly",
    "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x",
    "SELECT * FROM public.air_quality_hourly LIMIT (SELECT 1)",
    "SELECT * FROM public.air_quality_hourly FETCH FIRST 1 ROW WITH TIES",
    "SELECT 1 UNION SELECT 2",
    "```sql\nSELECT 1\n```",
])
def test_reject_unsafe_or_unsupported_sql(sql):
    with pytest.raises(ValueError):
        validate_sql(sql, TABLES, 100)


@pytest.mark.parametrize("sql", [
    GOOD_SQL,
    "SELECT aqi FROM air_quality_hourly WHERE aqi > 100 ORDER BY aqi DESC",
    "WITH x AS (SELECT aqi FROM public.air_quality_hourly) SELECT MAX(aqi) FROM x",
    "WITH air_quality_hourly AS (SELECT 1 AS aqi) SELECT * FROM public.air_quality_hourly",
    "WITH x AS (SELECT * FROM public.air_quality_hourly), y AS (SELECT * FROM x) SELECT * FROM y",
    "SELECT a.aqi FROM public.air_quality_hourly a JOIN public.air_quality_hourly b ON a.aqi=b.aqi",
    "SELECT EXTRACT(YEAR FROM observed_at), COUNT(*) FROM public.air_quality_hourly GROUP BY 1",
    "SELECT DATE_TRUNC('month', observed_at), AVG(aqi) FROM public.air_quality_hourly GROUP BY 1",
    "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY aqi) FROM public.air_quality_hourly",
    "SELECT PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY aqi) FROM public.air_quality_hourly",
    "SELECT * FROM public.air_quality_hourly WHERE observed_at >= DATE '2025-01-01'",
    "SELECT ROUND(AVG(aqi)::numeric, 2), COUNT(DISTINCT aqi) FROM public.air_quality_hourly",
    "SELECT CASE WHEN aqi > 100 THEN 1 ELSE 0 END FROM public.air_quality_hourly",
    "SELECT 'DELETE; DROP TABLE x' AS harmless_text",
    "SELECT 1 -- DROP TABLE x",
])
def test_accept_supported_sql(sql):
    validated = validate_sql(sql, TABLES, 100)
    assert parse_one(validated, read="postgres").args["limit"].expression.this == "101"


def test_limit_caps_only_outer_query_and_preserves_smaller_user_limit():
    sql = "SELECT AVG(aqi) FROM public.air_quality_hourly"
    tree = parse_one(validate_sql(sql, TABLES, 100), read="postgres")
    assert tree.args["limit"].expression.this == "101"
    assert "LIMIT 5" in validate_sql(sql + " LIMIT 5", TABLES, 100)
    assert "LIMIT 101" in validate_sql(sql + " LIMIT 900", TABLES, 100)


def test_unqualified_table_is_resolved_and_ambiguity_rejected():
    assert '"public".air_quality_hourly' in validate_sql("SELECT * FROM air_quality_hourly", TABLES, 10)
    with pytest.raises(ValueError):
        validate_sql("SELECT * FROM air_quality_hourly", TABLES + ("other.air_quality_hourly",), 10)


def test_quoted_identifiers_do_not_bypass_whitelist():
    with pytest.raises(ValueError):
        validate_sql('SELECT * FROM "PUBLIC".air_quality_hourly', TABLES, 10)
    assert "LIMIT" in validate_sql("SELECT * FROM PUBLIC.AIR_QUALITY_HOURLY", TABLES, 10)


def test_answer_preserves_values_and_reports_truncation():
    answer = format_answer({"columns": ["日期", "AQI", "備註"],
        "rows": [(date(2025, 1, 1), Decimal("100.0100"), None)], "truncated": True})
    assert "2025-01-01" in answer and "100.0100" in answer and "NULL" in answer
    assert "截斷" in answer
    assert "0" in format_answer({"columns": ["筆數"], "rows": [(0,)], "truncated": False})


def test_readonly_transaction_sets_local_protections():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    with readonly_connection(engine, 5000) as actual:
        assert actual is conn
    conn.begin.assert_called_once()
    assert conn.exec_driver_sql.call_args_list[0].args[0] == "SET TRANSACTION READ ONLY"
    assert conn.exec_driver_sql.call_args_list[1].args[0] == "SET LOCAL search_path = pg_catalog"
    assert [call.args[1] for call in conn.execute.call_args_list] == [{"v": "5000"}, {"v": "1000"}]


@pytest.fixture
def mocked_connection(monkeypatch):
    conn = MagicMock()

    @contextmanager
    def connection(*args):
        yield conn

    monkeypatch.setattr(database, "readonly_connection", connection)
    return conn


def settings():
    return Settings("fake-model", "postgresql+psycopg://fake", max_result_rows=2)


def test_executor_fetches_only_cap_plus_one(mocked_connection):
    result = mocked_connection.execution_options.return_value.exec_driver_sql.return_value.__enter__.return_value
    result.keys.return_value = ["aqi"]
    result.fetchmany.return_value = [(1,), (2,), (3,)]
    answer = execute_sql(Mock(), "SELECT aqi FROM t LIMIT 3", settings())
    mocked_connection.execution_options.assert_called_once_with(stream_results=True, no_parameters=True)
    result.fetchmany.assert_called_once_with(3)
    assert answer == {"columns": ["aqi"], "rows": [(1,), (2,)], "truncated": True}


def test_executor_captures_connection_failure(mocked_connection):
    mocked_connection.execution_options.side_effect = OperationalError(
        "SQL", {}, Exception("postgresql://user:secret@host"))
    with pytest.raises(DatabaseError, match="無法連線") as error:
        execute_sql(Mock(), GOOD_SQL, settings())
    assert "secret" not in str(error.value)


def test_schema_reads_actual_columns_and_checks_role(mocked_connection):
    mocked_connection.execute.side_effect = [
        Mock(scalar_one=Mock(return_value=False)),
        Mock(scalar_one=Mock(return_value=False)),
        Mock(all=Mock(return_value=[("real_column", "numeric(8,2)")])),
        Mock(mappings=Mock(return_value=Mock(one=Mock(return_value={
            "can_read": True, "can_write": False, "can_create": False})))),
    ]
    schema = load_schema(Mock(), settings())
    assert "real_column: numeric(8,2)" in schema
    assert "aqi" not in schema
    assert mocked_connection.execute.call_args_list[2].args[1] == {
        "schema": "public", "table": "air_quality_hourly"}


def test_schema_rejects_powerful_role(mocked_connection):
    mocked_connection.execute.side_effect = [
        Mock(scalar_one=Mock(return_value=True)), Mock(scalar_one=Mock(return_value=False))]
    with pytest.raises(DatabaseError, match="權限過大"):
        load_schema(Mock(), settings())


def test_database_sql_error_keeps_code_without_raw_credentials():
    original = Exception("sensitive row value / password")
    original.sqlstate = "42703"
    original.diag = SimpleNamespace(column_name="missing_aqi", table_name=None)
    error = database_error(DBAPIError("secret SQL", {}, original))
    assert error.kind == "query" and "42703" in str(error) and "missing_aqi" in str(error)
    assert "password" not in str(error) and "sensitive" not in str(error)


@pytest.mark.parametrize("value", ["pg_catalog.pg_authid", "public.x;DROP TABLE x", "public.MixedCase", ""])
def test_settings_reject_invalid_whitelist(monkeypatch, value):
    monkeypatch.setattr(config, "load_dotenv", lambda path: None)
    for key, val in {"LLM_MODEL": "fake", "DATABASE_URL":
                     "postgresql+psycopg://fake", "ALLOWED_TABLES": value}.items():
        monkeypatch.setenv(key, val)
    with pytest.raises(ValueError, match="ALLOWED_TABLES"):
        config.load_settings()


def test_settings_repr_hides_secrets():
    assert "postgresql" not in repr(settings())


@pytest.fixture
def ollama_settings_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda path: None)
    for key in ("OPENAI_API_KEY", "OLLAMA_BASE_URL", "LLM_TIMEOUT_SECONDS",
                "STATEMENT_TIMEOUT_MS", "MAX_RESULT_ROWS", "ALLOWED_TABLES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_MODEL", "gpt-oss:120b-cloud")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://fake")


def test_ollama_settings_do_not_require_openai_key(ollama_settings_env):
    loaded = config.load_settings()
    assert loaded.model == "gpt-oss:120b-cloud"
    assert loaded.ollama_base_url == "http://localhost:11434"
    assert loaded.llm_timeout_seconds == 120


def test_ollama_model_and_endpoint_can_be_changed(ollama_settings_env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "nemotron-3-super:cloud")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "240")
    loaded = config.load_settings()
    assert loaded.model == "nemotron-3-super:cloud"
    assert loaded.ollama_base_url == "http://127.0.0.1:11434"
    assert loaded.llm_timeout_seconds == 240


@pytest.mark.parametrize("endpoint", ["", "localhost:11434", "ftp://localhost:11434",
    "http://localhost:11434/api", "http://user:secret@localhost:11434",
    "http://localhost:11434?x=1", "http://localhost:invalid"])
def test_ollama_rejects_invalid_endpoint(ollama_settings_env, monkeypatch, endpoint):
    monkeypatch.setenv("OLLAMA_BASE_URL", endpoint)
    with pytest.raises(ValueError, match="OLLAMA_BASE_URL"):
        config.load_settings()


@pytest.mark.parametrize("timeout", ["0", "601", "abc"])
def test_ollama_rejects_invalid_timeout(ollama_settings_env, monkeypatch, timeout):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ValueError, match="LLM"):
        config.load_settings()


def test_ollama_native_response_supplies_only_final_sql(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        body = {"model": "gpt-oss:120b-cloud", "done": True,
                "message": {"role": "assistant", "content": GOOD_SQL,
                            "thinking": "Reasoning must not become SQL"}}
        return httpx.Response(200, content=json.dumps(body) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})

    real_chat = config.ChatOllama
    monkeypatch.setattr(config, "ChatOllama", lambda **kwargs: real_chat(**{
        **kwargs, "client_kwargs": {**kwargs["client_kwargs"],
                                    "transport": httpx.MockTransport(handler)}}))
    llm = config.create_llm(Settings("gpt-oss:120b-cloud", "postgresql+psycopg://fake"))
    execute = Mock(return_value=RESULT)
    workflow = build_graph(llm, SCHEMA, TABLES, execute)
    state = workflow.invoke(initial_state("查詢 AQI 最大值"))
    assert state["status"] == "success" and state["retry_count"] == 0
    assert "Reasoning" not in state["sql_query"]
    assert requests[0]["model"] == "gpt-oss:120b-cloud"
    assert requests[0]["messages"][0]["role"] == "system"
    execute.assert_called_once()


@pytest.mark.parametrize("code, kind", [("57014", "query"), ("42501", "permission"),
                                       ("08006", "connection"), ("28P01", "connection")])
def test_database_error_categories(code, kind):
    original = Exception("sensitive")
    original.sqlstate = code
    assert database_error(DBAPIError("SQL", {}, original)).kind == kind


@pytest.mark.parametrize("rights", [
    {"can_read": False, "can_write": False, "can_create": False},
    {"can_read": True, "can_write": True, "can_create": False},
    {"can_read": True, "can_write": False, "can_create": True},
])
def test_schema_rejects_invalid_table_privileges(mocked_connection, rights):
    mocked_connection.execute.side_effect = [
        Mock(scalar_one=Mock(return_value=False)), Mock(scalar_one=Mock(return_value=False)),
        Mock(all=Mock(return_value=[("aqi", "integer")])),
        Mock(mappings=Mock(return_value=Mock(one=Mock(return_value=rights)))),
    ]
    with pytest.raises(DatabaseError, match="唯讀要求"):
        load_schema(Mock(), settings())


def test_each_question_has_fresh_state():
    llm, execute = Mock(), Mock(return_value=RESULT)
    llm.invoke.side_effect = [AIMessage(content="SELECT ("), AIMessage(content=GOOD_SQL),
                              AIMessage(content=GOOD_SQL)]
    workflow = build_graph(llm, SCHEMA, TABLES, execute)
    first = workflow.invoke(initial_state("第一次"))
    second = workflow.invoke(initial_state("第二次"))
    assert first["retry_count"] == 1 and second["retry_count"] == 0


def test_gradio_build_and_callback_with_mocked_dependencies(monkeypatch):
    import app

    llm = Mock()
    llm.invoke.return_value = AIMessage(content=GOOD_SQL)
    monkeypatch.setattr(app, "load_settings", settings)
    monkeypatch.setattr(app, "create_db_engine", lambda _: Mock())
    monkeypatch.setattr(app, "load_schema", lambda *_: SCHEMA)
    monkeypatch.setattr(app, "create_llm", lambda _: llm)
    monkeypatch.setattr(app, "execute_sql", lambda *_: RESULT)
    demo = app.create_app()
    submit = next(fn.fn for fn in demo.fns.values() if fn.fn.__name__ == "submit")
    answer, sql, status = submit("測試問題")
    assert "123" in answer and "LIMIT 3" in sql and "成功" in status
    assert demo.config["components"]


def test_gradio_configuration_failure_is_visible(monkeypatch):
    import app

    monkeypatch.setattr(app, "load_settings", Mock(side_effect=ValueError("缺少環境變數")))
    demo = app.create_app()
    submit = next(fn.fn for fn in demo.fns.values() if fn.fn.__name__ == "submit")
    answer, sql, status = submit("問題")
    assert "缺少環境變數" in answer and not sql and "失敗" in status
