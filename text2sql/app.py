"""Local data-query workspace. Run with python app.py."""

import os

# Disable Gradio telemetry; no public sharing is enabled by this application.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr

from config import create_llm, load_settings
from database import DatabaseError, create_db_engine, execute_sql, load_schema
from graph import GRAPH_RECURSION_LIMIT, build_graph, initial_state
from ui import (APP_CSS, APP_THEME, TABLE_COPY_JS, header_html, hero_html,
                scope_header_html, section_html, tables_html)


# 初始化查詢服務並建立 Gradio 操作介面。
def create_app() -> gr.Blocks:
    settings = None
    engine = None
    workflow = None
    startup_error = None
    try:
        settings = load_settings()
        engine = create_db_engine(settings)
        cached_schema = load_schema(engine, settings)
        workflow = build_graph(
            create_llm(settings), cached_schema, settings.allowed_tables,
            lambda sql: execute_sql(engine, sql, settings), settings.max_result_rows,
        )
    except (ValueError, DatabaseError) as exc:
        startup_error = str(exc)
    except Exception:
        startup_error = "初始化失敗，請檢查環境變數、套件與資料庫設定。"

    # 執行問題查詢，回傳答案、SQL 與修正狀態供介面顯示。
    def submit(question: str) -> tuple[str, str, str]:
        if workflow is None:
            return f"啟動失敗：{startup_error}", "", "失敗｜修正 0 次"
        try:
            state = workflow.invoke(initial_state(question),
                                    config={"recursion_limit": GRAPH_RECURSION_LIMIT})
            label = "成功" if state["status"] == "success" else "失敗"
            return (state["final_answer"], state["sql_query"],
                    f"{label}｜修正 {state['retry_count']} 次")
        except Exception:
            return "流程發生未預期錯誤，請檢查程式與設定。", "", "失敗"

    tables = settings.allowed_tables if settings else ()
    max_rows = settings.max_result_rows if settings else 100
    examples = [
        ["請顯示 xx資料表 的前 5 筆資料。"],
        ["請統計 xx資料表 總共有幾筆資料。"],
        ["請依 xx資料表 的 xx欄位 分組，統計每組筆數。"],
    ]

    with gr.Blocks(title="Mini Text-to-SQL 查詢助手") as demo:
        with gr.Column(elem_id="query-app", min_width=0):
            gr.HTML(header_html(workflow is not None), padding=False, elem_id="workspace-header")
            gr.HTML(hero_html(len(tables), max_rows), padding=False, elem_id="workspace-hero")
            if startup_error:
                # Plain text prevents database identifiers from becoming Markdown/HTML.
                gr.Textbox(value=f"啟動失敗：{startup_error}；修正設定後請重新啟動。",
                           label="啟動狀態", interactive=False, elem_id="query-startup")
            with gr.Row(elem_id="query-workspace"):
                with gr.Column(scale=3, min_width=0, elem_id="workspace-main"):
                    with gr.Column(min_width=0, elem_id="question-card"):
                        gr.HTML(section_html("01", "提出查詢", "設定問題與條件，從資料中找到答案。"),
                                padding=False)
                        question = gr.Textbox(
                            label="查詢問題", lines=3, elem_id="query-question",
                            placeholder="想查詢什麼？例如：請顯示 xx資料表 的前 5 筆資料。",
                        )
                        gr.Examples(
                            examples=examples, inputs=question, cache_examples=False,
                            run_on_click=False, examples_per_page=3,
                            label="從範例開始 · 點選帶入後可修改", elem_id="query-examples",
                        )
                        with gr.Row(elem_id="query-actions"):
                            button = gr.Button(
                                "送出查詢 →", variant="primary", size="md", scale=0, min_width=144,
                                elem_id="query-submit", interactive=workflow is not None,
                            )
                    with gr.Column(min_width=0, elem_id="results-card"):
                        gr.HTML(section_html("02", "查詢輸出", "檢視結果，也能切換查看實際 SQL。"),
                                padding=False)
                        with gr.Tabs(elem_id="query-results-tabs"):
                            with gr.Tab("查詢結果", id="answer"):
                                answer = gr.Textbox(
                                    label="查詢結果", show_label=False, lines=8, interactive=False,
                                    placeholder="尚無查詢結果。送出問題後，結果會顯示在這裡。",
                                    elem_id="query-answer", buttons=["copy"],
                                )
                            with gr.Tab("SQL 檢視", id="sql"):
                                sql = gr.Code(
                                    label="SQL（成功時為實際執行版本；失敗時為最後一版）",
                                    language="sql", interactive=False, elem_id="query-sql",
                                )
                        status = gr.Textbox(
                            value="尚未執行", label="執行狀態及修正次數", interactive=False,
                            elem_id="query-status",
                        )
                with gr.Column(scale=1, min_width=0, elem_id="workspace-aside"):
                    with gr.Column(min_width=0, elem_id="scope-card"):
                        gr.HTML(scope_header_html(len(tables)), padding=False)
                        table_search = gr.Textbox(
                            label="搜尋資料表", placeholder="搜尋資料表或 Schema 名稱",
                            elem_id="query-table-search", interactive=True,
                        )
                        table_list = gr.HTML(
                            tables_html(tables), padding=False, js_on_load=TABLE_COPY_JS,
                        )
                        table_search.change(
                            lambda query: tables_html(tables, query),
                            inputs=table_search, outputs=table_list,
                            queue=False, show_progress="hidden", api_name="filter_tables",
                        )
                    with gr.Accordion("使用說明與限制", open=True, elem_id="query-guide"):
                        gr.Markdown(
                            "- 輸入問題或點選範例，指定資料表與條件後按「送出查詢」。\n"
                            f"- **僅限唯讀**，不能新增、修改或刪除資料；每次最多顯示 **{max_rows} 筆**。\n"
                            "- 每次提問互相獨立；只使用資料庫現有的資料與欄位。\n"
                            "- 不支援跨資料庫、UNION、視窗函式或遞迴查詢；結果請核對。"
                        )
            button.click(submit, inputs=question, outputs=[answer, sql, status],
                         concurrency_limit=1, show_progress="minimal")
            gr.HTML('<p class="workspace-footer">Mini Text-to-SQL · 資料查詢工作台</p>',
                    padding=False)
    demo.queue()
    return demo


if __name__ == "__main__":
    create_app().launch(server_name="127.0.0.1", share=False, show_error=False,
                        css=APP_CSS, theme=APP_THEME, footer_links=[])
