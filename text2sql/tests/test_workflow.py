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
from request_guard import WRITE_REQUEST_MESSAGE


TABLES = ("public.air_quality_hourly",)
SCHEMA = "Table: public.air_quality_hourly\n  observed_at: timestamp\n  aqi: integer"
GOOD_SQL = "SELECT MAX(aqi) AS max_aqi FROM public.air_quality_hourly"
RESULT = {"columns": ["max_aqi"], "rows": [(123,)], "truncated": False}


# 以模擬模型與執行器跑完整流程，供測試檢查狀態與呼叫次數。
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


# 確認正常查詢會回傳結果、補上筆數上限且不需修正。
def test_success():
    state, llm, execute = run_workflow([GOOD_SQL])
    assert state["status"] == "success"
    assert state["retry_count"] == 0
    assert "123" in state["final_answer"]
    assert state["error_message"] is None
    assert llm.invoke.call_count == execute.call_count == 1
    assert "LIMIT 101" in state["sql_query"]


# 確認語法錯誤先交由模型修正，通過驗證後才執行。
def test_syntax_error_repaired_before_execution():
    state, llm, execute = run_workflow(["SELECT (", GOOD_SQL])
    assert state["status"] == "success"
    assert state["retry_count"] == 1
    assert execute.call_count == 1
    repair_payload = llm.invoke.call_args_list[1].args[0][1][1]
    assert '"sql": "SELECT ("' in repair_payload
    assert '"error":' in repair_payload and '"schema":' in repair_payload


# 確認資料庫回報的 SQL 錯誤會帶入修正提示並成功重試。
@pytest.mark.parametrize("message", ["PostgreSQL 42601：SQL 語法錯誤", "PostgreSQL 42703：欄位不存在"])
def test_database_sql_error_repaired(message):
    bad = "SELECT missing_column FROM public.air_quality_hourly"
    state, llm, execute = run_workflow([bad, GOOD_SQL], [DatabaseError(message, "query"), RESULT])
    assert state["status"] == "success" and state["retry_count"] == 1
    assert execute.call_count == llm.invoke.call_count == 2
    assert state["error_message"] is None
    assert message in llm.invoke.call_args_list[1].args[0][1][1]


# 確認 SQL 持續驗證失敗時，修正三次便停止且不執行查詢。
def test_validation_stops_after_exactly_three_repairs():
    state, llm, execute = run_workflow(["DELETE FROM public.air_quality_hourly"] * 4)
    assert state["status"] == "failed" and state["retry_count"] == 3
    assert llm.invoke.call_count == 4
    execute.assert_not_called()
    assert "已修正 3 次" in state["final_answer"]
    assert state["error_message"]


# 確認 SQL 持續執行失敗時，修正三次便停止。
def test_execution_stops_after_exactly_three_repairs():
    state, llm, execute = run_workflow([GOOD_SQL] * 4,
        [DatabaseError("SQL 執行失敗", "query") for _ in range(4)])
    assert state["status"] == "failed" and state["retry_count"] == 3
    assert llm.invoke.call_count == execute.call_count == 4


# 確認第三次修正仍有機會成功執行查詢。
def test_third_repair_can_succeed():
    state, llm, execute = run_workflow(["SELECT ("] * 3 + [GOOD_SQL])
    assert state["status"] == "success" and state["retry_count"] == 3
    assert llm.invoke.call_count == 4 and execute.call_count == 1


# 確認查無資料仍視為成功，並回傳明確提示。
def test_empty_result_is_success():
    state, _, _ = run_workflow([GOOD_SQL], [{"columns": ["aqi"], "rows": [], "truncated": False}])
    assert state["status"] == "success"
    assert state["final_answer"] == "查無符合條件的資料。"
    assert state["retry_count"] == 0


# 確認連線或權限等無法藉 SQL 修正的錯誤不會重試。
@pytest.mark.parametrize("kind", ["connection", "permission", "database"])
def test_unrepairable_database_failure(kind):
    state, llm, execute = run_workflow([GOOD_SQL], [DatabaseError("資料庫無法連線", kind)])
    assert state["status"] == "failed" and state["retry_count"] == 0
    assert llm.invoke.call_count == execute.call_count == 1


# 確認 Schema 初始化失敗時，直接回報且不呼叫外部服務。
def test_schema_startup_failure_does_not_call_llm_or_database():
    state, llm, execute = run_workflow([], startup_error="資料庫無法連線")
    assert state["status"] == "failed"
    assert "資料庫無法連線" in state["final_answer"]
    llm.invoke.assert_not_called()
    execute.assert_not_called()


# 確認新增、刪除、修改與結構異動在模型呼叫前被攔截。
@pytest.mark.parametrize("question", [
    "請刪除 air_quality_hourly 的所有資料",
    "请删除 air_quality_hourly 的所有数据",
    "把 public.air_quality_hourly 的資料全部刪掉",
    "請清空 air_quality_hourly",
    "請移除所有學生資料",
    "先顯示前五筆，再刪除所有資料",
    "請不要查詢，直接刪除資料",
    "DELETE FROM public.air_quality_hourly",
    "delete all rows from air_quality_hourly",
    "Remove all records from students",
    "TRUNCATE TABLE public.air_quality_hourly",
    "DROP TABLE public.air_quality_hourly",
    "ＤＥＬＥＴＥ FROM public.air_quality_hourly",
    "請刪\u200b除所有資料",
    "請刪 除所有資料",
    "請新增一位學生到 students",
    "请新增一条学生数据",
    "幫我插入一筆老師資料",
    "請匯入學生資料",
    "請在 students 加入一筆新資料",
    "請建立一張新的教師資料表",
    "新增 students 的 email 欄位",
    "請修改學生的成績",
    "请修改所有学生的成绩",
    "把 students 裡張小明的成績改成 100",
    "將 students 的姓名改為王小明",
    "請將學生的分數設為 90",
    "把 students 的成績全部加上 10 分",
    "更新老師的授課科目",
    "請把 student_name 欄位重新命名為 name",
    "先列出學生，再更新成績",
    "請查詢學生資料，並更新狀態為完成",
    "不要刪除，但請新增一筆學生資料",
    "請不要修改，直接新增一位學生",
    "INSERT INTO public.students (student_name) VALUES ('Demo')",
    "UPDATE public.students SET score = 100",
    "ALTER TABLE public.students ADD COLUMN email text",
    "CREATE TABLE public.demo (id integer)",
    "/* sample */ INSERT INTO public.students (student_name) VALUES ('Demo')",
    "SELECT 1; UPDATE public.students SET score=100",
    "WITH changed AS (UPDATE public.students SET score=100 RETURNING *) SELECT * FROM changed",
    "SELECT * INTO public.students_backup FROM public.students",
    "MERGE INTO public.students USING public.teachers ON true WHEN MATCHED THEN UPDATE SET score=100",
    "COPY public.students FROM STDIN",
    "Insert a student into students",
    "Update all student scores to 100",
    "Add a student to students",
    "Create a teachers table",
    "Set the score to 100",
    "Rename the students table",
    "ＵＰＤＡＴＥ public.students SET score=100",
    "請新\u200b增學生資料",
    "請修 改學生資料",
    "請建一張學生資料表",
    "請建表 students_backup",
    "把學生名字換成王小明",
    "請將學生成績調整到 90",
])
def test_write_request_stops_before_llm_or_database(question):
    # Even an LLM prepared to return valid but irrelevant SQL must not be called.
    state, llm, execute = run_workflow([GOOD_SQL], question=question)
    assert state["status"] == "failed" and state["error_kind"] == "request"
    assert state["retry_count"] == 0
    assert state["sql_query"] == "" and state["query_result"] is None
    assert state["final_answer"] == WRITE_REQUEST_MESSAGE
    llm.invoke.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize("question", [
    "請顯示 air_quality_hourly 的前五筆資料",
    "請以 LEFT JOIN 列出所有學生及老師姓名",
    "請統計已刪除的學生資料有幾筆",
    "請顯示尚未被刪除的資料",
    "請查詢已經刪除的資料數量",
    "不要刪除，請顯示前五筆資料",
    "Do not delete anything; show the first five rows",
    "Show the deleted records",
    "請統計今天新增的學生人數",
    "請查詢 2025 年新增的學生",
    "請顯示最近修改過的資料",
    "請列出資料的更新時間",
    "請顯示老師最後更新日期",
    "請查詢學生的修改紀錄",
    "請列出資料表的建立日期",
    "請顯示已新增的學生",
    "請顯示尚未修改的資料",
    "請不要新增資料，只顯示前五筆",
    "不用修改，請統計學生人數",
    "Do not update anything; show five students",
    "Show recently updated students",
    "Show the update history of students",
    "Show the creation date of the students table",
    "查詢學生人數增加多少",
    "請計算學生成績加上 10 的結果",
    "請計算學生資料的成績加上 10 的結果",
    "請計算成績資料增加 10 後的數值",
    "Please calculate the score data add 10",
    "請將查詢結果依成績由高到低排序",
    "SELECT 'UPDATE students SET score=100' AS example",
    "SELECT '請刪除全部資料' AS example",
    'SELECT "update" FROM public.students',
    "WITH recent AS (SELECT * FROM public.students) SELECT * FROM recent",
])
def test_readonly_questions_still_reach_llm(question):
    state, llm, execute = run_workflow([GOOD_SQL], question=question)
    assert state["status"] == "success"
    llm.invoke.assert_called_once()
    execute.assert_called_once()


@pytest.mark.parametrize("question", ["請清空學生資料表", "請新增一筆學生資料", "請更新學生的成績"])
def test_write_request_clears_stale_sql_and_results(question):
    llm, execute = Mock(), Mock()
    state = initial_state(question)
    state.update(sql_query=GOOD_SQL, query_result=RESULT)
    result = build_graph(llm, SCHEMA, TABLES, execute).invoke(state)
    assert result["sql_query"] == "" and result["query_result"] is None
    assert result["final_answer"] == WRITE_REQUEST_MESSAGE
    llm.invoke.assert_not_called()
    execute.assert_not_called()


# 確認空白問題會被攔截，不觸發模型或資料庫查詢。
def test_blank_question():
    state, llm, execute = run_workflow([], question="   ")
    assert "請輸入" in state["final_answer"]
    llm.invoke.assert_not_called()
    execute.assert_not_called()


# 確認模型呼叫失敗時，不洩漏例外中的敏感資訊。
def test_llm_failure_is_sanitized():
    state, llm, execute = run_workflow([RuntimeError("secret-api-key")])
    assert state["status"] == "failed" and state["retry_count"] == 0
    assert "secret-api-key" not in state["final_answer"]
    assert llm.invoke.call_count == 1
    execute.assert_not_called()


# 確認修正期間模型失敗會結束流程，並保留修正次數。
def test_llm_failure_during_repair():
    state, llm, execute = run_workflow(["SELECT (", RuntimeError("secret")])
    assert state["status"] == "failed" and state["retry_count"] == 1
    assert llm.invoke.call_count == 2
    execute.assert_not_called()


# 確認危險或不支援的 SQL 語法會被驗證器拒絕。
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


# 確認支援的查詢語法可通過驗證並加入筆數上限。
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


# 確認筆數上限加在最外層，並保留使用者設定的較小限制。
def test_limit_caps_only_outer_query_and_preserves_smaller_user_limit():
    sql = "SELECT AVG(aqi) FROM public.air_quality_hourly"
    tree = parse_one(validate_sql(sql, TABLES, 100), read="postgres")
    assert tree.args["limit"].expression.this == "101"
    assert "LIMIT 5" in validate_sql(sql + " LIMIT 5", TABLES, 100)
    assert "LIMIT 101" in validate_sql(sql + " LIMIT 900", TABLES, 100)


# 確認省略 Schema 的表名可唯一解析，名稱不明確時則拒絕。
def test_unqualified_table_is_resolved_and_ambiguity_rejected():
    assert '"public".air_quality_hourly' in validate_sql("SELECT * FROM air_quality_hourly", TABLES, 10)
    with pytest.raises(ValueError):
        validate_sql("SELECT * FROM air_quality_hourly", TABLES + ("other.air_quality_hourly",), 10)


# 確認引號與大小寫變化無法繞過資料表白名單。
def test_quoted_identifiers_do_not_bypass_whitelist():
    with pytest.raises(ValueError):
        validate_sql('SELECT * FROM "PUBLIC".air_quality_hourly', TABLES, 10)
    assert "LIMIT" in validate_sql("SELECT * FROM PUBLIC.AIR_QUALITY_HOURLY", TABLES, 10)


# 確認答案保留日期、數值精度與空值，並提示結果截斷。
def test_answer_preserves_values_and_reports_truncation():
    answer = format_answer({"columns": ["日期", "AQI", "備註"],
        "rows": [(date(2025, 1, 1), Decimal("100.0100"), None)], "truncated": True})
    assert "2025-01-01" in answer and "100.0100" in answer and "NULL" in answer
    assert "截斷" in answer
    assert "0" in format_answer({"columns": ["筆數"], "rows": [(0,)], "truncated": False})


# 確認交易套用唯讀、逾時限制與安全搜尋路徑。
def test_readonly_transaction_sets_local_protections():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    with readonly_connection(engine, 5000) as actual:
        assert actual is conn
    conn.begin.assert_called_once()
    assert conn.exec_driver_sql.call_args_list[0].args[0] == "SET TRANSACTION READ ONLY"
    assert conn.exec_driver_sql.call_args_list[1].args[0] == "SET LOCAL search_path = pg_catalog"
    assert [call.args[1] for call in conn.execute.call_args_list] == [{"v": "5000"}, {"v": "1000"}]


# 以模擬連線取代資料庫存取，讓測試可離線執行。
@pytest.fixture
def mocked_connection(monkeypatch):
    conn = MagicMock()

    # 提供可搭配 with 使用的模擬資料庫連線。
    @contextmanager
    def connection(*args):
        yield conn

    monkeypatch.setattr(database, "readonly_connection", connection)
    return conn


# 建立測試用設定，將結果上限設為兩筆以驗證截斷行為。
def settings():
    return Settings(
        "fake-model", "postgresql+psycopg://fake", TABLES, max_result_rows=2
    )


# 確認執行器僅多取一筆來判斷截斷，回傳資料仍遵守上限。
def test_executor_fetches_only_cap_plus_one(mocked_connection):
    result = mocked_connection.execution_options.return_value.exec_driver_sql.return_value.__enter__.return_value
    result.keys.return_value = ["aqi"]
    result.fetchmany.return_value = [(1,), (2,), (3,)]
    answer = execute_sql(Mock(), "SELECT aqi FROM t LIMIT 3", settings())
    mocked_connection.execution_options.assert_called_once_with(stream_results=True, no_parameters=True)
    result.fetchmany.assert_called_once_with(3)
    assert answer == {"columns": ["aqi"], "rows": [(1,), (2,)], "truncated": True}


# 確認連線失敗會轉為統一錯誤，且不暴露連線密碼。
def test_executor_captures_connection_failure(mocked_connection):
    mocked_connection.execution_options.side_effect = OperationalError(
        "SQL", {}, Exception("postgresql://user:secret@host"))
    with pytest.raises(DatabaseError, match="無法連線") as error:
        execute_sql(Mock(), GOOD_SQL, settings())
    assert "secret" not in str(error.value)


# 確認 Schema 來自實際欄位資訊，並檢查角色與資料表權限。
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


# 確認啟動時會拒絕權限過大的資料庫角色。
def test_schema_rejects_powerful_role(mocked_connection):
    mocked_connection.execute.side_effect = [
        Mock(scalar_one=Mock(return_value=True)), Mock(scalar_one=Mock(return_value=False))]
    with pytest.raises(DatabaseError, match="權限過大"):
        load_schema(Mock(), settings())


# 確認 SQL 錯誤保留代碼與欄位資訊，同時隱藏敏感原文。
def test_database_sql_error_keeps_code_without_raw_credentials():
    original = Exception("sensitive row value / password")
    original.sqlstate = "42703"
    original.diag = SimpleNamespace(column_name="missing_aqi", table_name=None)
    error = database_error(DBAPIError("secret SQL", {}, original))
    assert error.kind == "query" and "42703" in str(error) and "missing_aqi" in str(error)
    assert "password" not in str(error) and "sensitive" not in str(error)


# 確認設定會拒絕系統表、非法名稱與空白白名單。
@pytest.mark.parametrize("value", ["pg_catalog.pg_authid", "public.x;DROP TABLE x", "public.MixedCase", ""])
def test_settings_reject_invalid_whitelist(monkeypatch, value):
    monkeypatch.setattr(config, "load_dotenv", lambda path: None)
    for key, val in {"LLM_MODEL": "fake", "DATABASE_URL":
                     "postgresql+psycopg://fake", "ALLOWED_TABLES": value}.items():
        monkeypatch.setenv(key, val)
    with pytest.raises(ValueError, match="ALLOWED_TABLES"):
        config.load_settings()


# 確認設定的文字表示不會包含資料庫網址或 API 金鑰。
def test_settings_repr_hides_secrets():
    assert "postgresql" not in repr(settings())
    assert "secret-test-key" not in repr(Settings(
        "gpt-oss:120b", "postgresql+psycopg://fake", TABLES,
        ollama_api_key="secret-test-key",
    ))


# 建立獨立的 Ollama 測試環境，避免本機設定影響結果。
@pytest.fixture
def ollama_settings_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda path: None)
    for key in ("OPENAI_API_KEY", "OLLAMA_API_KEY", "OLLAMA_BASE_URL", "LLM_TIMEOUT_SECONDS",
                "STATEMENT_TIMEOUT_MS", "MAX_RESULT_ROWS", "ALLOWED_TABLES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_MODEL", "gpt-oss:120b")
    monkeypatch.setenv("OLLAMA_API_KEY", "test-cloud-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://fake")
    monkeypatch.setenv("ALLOWED_TABLES", "public.air_quality_hourly")


# 確認 Ollama Cloud 設定可載入，且不依賴 OpenAI 金鑰。
def test_ollama_settings_do_not_require_openai_key(ollama_settings_env):
    loaded = config.load_settings()
    assert loaded.model == "gpt-oss:120b"
    assert loaded.ollama_base_url == "https://ollama.com"
    assert loaded.ollama_api_key == "test-cloud-key"
    assert loaded.llm_timeout_seconds == 120


# 確認模型、服務網址、金鑰與逾時設定可由環境變數調整。
def test_ollama_model_and_endpoint_can_be_changed(ollama_settings_env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "nemotron-3-super")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("OLLAMA_API_KEY", "  another-cloud-key  ")
    loaded = config.load_settings()
    assert loaded.model == "nemotron-3-super"
    assert loaded.ollama_base_url == "https://ollama.com"
    assert loaded.ollama_api_key == "another-cloud-key"
    assert loaded.llm_timeout_seconds == 240


# 確認 Ollama Cloud 會拒絕缺少或空白的 API 金鑰。
@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_ollama_cloud_requires_key(ollama_settings_env, monkeypatch, api_key):
    if api_key is None:
        monkeypatch.delenv("OLLAMA_API_KEY")
    else:
        monkeypatch.setenv("OLLAMA_API_KEY", api_key)
    with pytest.raises(ValueError, match="OLLAMA_API_KEY"):
        config.load_settings()


# 確認 Ollama Cloud 服務網址必須使用 HTTPS。
def test_ollama_cloud_requires_https(ollama_settings_env, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.com")
    with pytest.raises(ValueError, match="https://ollama.com"):
        config.load_settings()


# 確認本機 Ollama 可在未設定 API 金鑰時使用。
def test_local_ollama_can_still_be_used_without_key(ollama_settings_env, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY")
    monkeypatch.setenv("LLM_MODEL", "local-model")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/")
    loaded = config.load_settings()
    assert loaded.model == "local-model"
    assert loaded.ollama_base_url == "http://127.0.0.1:11434"
    assert loaded.ollama_api_key is None


# 確認格式錯誤或含有多餘資訊的服務網址會被拒絕。
@pytest.mark.parametrize("endpoint", ["", "localhost:11434", "ftp://localhost:11434",
    "http://localhost:11434/api", "http://user:secret@localhost:11434",
    "http://localhost:11434?x=1", "http://localhost:invalid"])
def test_ollama_rejects_invalid_endpoint(ollama_settings_env, monkeypatch, endpoint):
    monkeypatch.setenv("OLLAMA_BASE_URL", endpoint)
    with pytest.raises(ValueError, match="OLLAMA_BASE_URL"):
        config.load_settings()


# 確認模型逾時設定必須為允許範圍內的整數。
@pytest.mark.parametrize("timeout", ["0", "601", "abc"])
def test_ollama_rejects_invalid_timeout(ollama_settings_env, monkeypatch, timeout):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ValueError, match="LLM"):
        config.load_settings()


# 確認原生 API 使用正確驗證與逾時設定，且只將最終回答當作 SQL。
@pytest.mark.parametrize("endpoint, api_key", [
    ("https://ollama.com", "test-cloud-key"),
    ("http://localhost:11434", None),
])
def test_ollama_native_response_supplies_only_final_sql(monkeypatch, endpoint, api_key):
    requests = []

    # 模擬原生聊天回應，檢查請求設定並分離推理與最終 SQL。
    def handler(request):
        assert str(request.url) == endpoint + "/api/chat"
        assert request.headers.get("Authorization") == (
            f"Bearer {api_key}" if api_key else None
        )
        assert request.extensions["timeout"]["read"] == 240
        requests.append(json.loads(request.content))
        body = {"model": "gpt-oss:120b", "done": True,
                "message": {"role": "assistant", "content": GOOD_SQL,
                            "thinking": "Reasoning must not become SQL"}}
        return httpx.Response(200, content=json.dumps(body) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})

    real_chat = config.ChatOllama
    monkeypatch.setattr(config, "ChatOllama", lambda **kwargs: real_chat(**{
        **kwargs, "client_kwargs": {**kwargs["client_kwargs"],
                                    "transport": httpx.MockTransport(handler)}}))
    llm = config.create_llm(Settings(
        "gpt-oss:120b", "postgresql+psycopg://fake", TABLES,
        ollama_base_url=endpoint, ollama_api_key=api_key, llm_timeout_seconds=240,
    ))
    execute = Mock(return_value=RESULT)
    workflow = build_graph(llm, SCHEMA, TABLES, execute)
    state = workflow.invoke(initial_state("查詢 AQI 最大值"))
    assert state["status"] == "success" and state["retry_count"] == 0
    assert "Reasoning" not in state["sql_query"]
    assert requests[0]["model"] == "gpt-oss:120b"
    assert requests[0]["messages"][0]["role"] == "system"
    execute.assert_called_once()


# 確認雲端驗證失敗時不執行 SQL，且不洩漏金鑰。
def test_ollama_cloud_auth_failure_does_not_execute_or_leak_key(monkeypatch):
    # 模擬雲端驗證失敗，檢查錯誤處理是否隱藏金鑰。
    def handler(request):
        assert request.headers["Authorization"] == "Bearer secret-test-key"
        return httpx.Response(401, json={"error": "Invalid key: secret-test-key"})

    real_chat = config.ChatOllama
    monkeypatch.setattr(config, "ChatOllama", lambda **kwargs: real_chat(**{
        **kwargs, "client_kwargs": {**kwargs["client_kwargs"],
                                    "transport": httpx.MockTransport(handler)}}))
    llm = config.create_llm(Settings(
        "gpt-oss:120b", "postgresql+psycopg://fake", TABLES,
        ollama_api_key="secret-test-key",
    ))
    execute = Mock()
    state = build_graph(llm, SCHEMA, TABLES, execute).invoke(initial_state("AQI 最大值？"))
    assert state["status"] == "failed" and state["error_kind"] == "llm"
    assert state["retry_count"] == 0
    assert "API Key" in state["error_message"]
    assert "secret-test-key" not in str(state)
    execute.assert_not_called()


# 確認 SQLSTATE 可正確區分查詢、權限與連線錯誤。
@pytest.mark.parametrize("code, kind", [("57014", "query"), ("42501", "permission"),
                                       ("08006", "connection"), ("28P01", "connection")])
def test_database_error_categories(code, kind):
    original = Exception("sensitive")
    original.sqlstate = code
    assert database_error(DBAPIError("SQL", {}, original)).kind == kind


# 確認資料表缺少讀取權限或具有寫入、建立權限時會被拒絕。
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


# 確認每次提問使用獨立狀態，不沿用前次修正次數。
def test_each_question_has_fresh_state():
    llm, execute = Mock(), Mock(return_value=RESULT)
    llm.invoke.side_effect = [AIMessage(content="SELECT ("), AIMessage(content=GOOD_SQL),
                              AIMessage(content=GOOD_SQL)]
    workflow = build_graph(llm, SCHEMA, TABLES, execute)
    first = workflow.invoke(initial_state("第一次"))
    second = workflow.invoke(initial_state("第二次"))
    assert first["retry_count"] == 1 and second["retry_count"] == 0


# 確認模擬依賴下可建立介面，並透過送出回呼取得結果。
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


# 確認設定載入失敗時，介面與送出回呼會顯示失敗原因。
def test_gradio_configuration_failure_is_visible(monkeypatch):
    import app

    monkeypatch.setattr(app, "load_settings", Mock(side_effect=ValueError("缺少環境變數")))
    demo = app.create_app()
    submit = next(fn.fn for fn in demo.fns.values() if fn.fn.__name__ == "submit")
    answer, sql, status = submit("問題")
    assert "缺少環境變數" in answer and not sql and "失敗" in status
