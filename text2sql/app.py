"""Minimal local Gradio UI. Run with python app.py."""

import os

# Disable Gradio telemetry; no public sharing is enabled by this application.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr

from config import create_llm, load_settings
from database import DatabaseError, create_db_engine, execute_sql, load_schema
from graph import GRAPH_RECURSION_LIMIT, build_graph, initial_state


def create_app() -> gr.Blocks:
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

    with gr.Blocks(title="Mini Text-to-SQL 中文查詢助手") as demo:
        gr.Markdown("# Mini Text-to-SQL\n輸入中文問題，查詢指定的 PostgreSQL 資料表。")
        if startup_error:
            # Plain text prevents database identifiers from becoming Markdown/HTML.
            gr.Textbox(value=f"啟動失敗：{startup_error}；修正設定後請重新啟動。",
                       label="啟動狀態", interactive=False)
        question = gr.Textbox(label="中文問題", placeholder="例如：哪一個月 AQI 平均值最高？")
        button = gr.Button("送出查詢", variant="primary", interactive=workflow is not None)
        answer = gr.Textbox(label="中文回答", lines=8, interactive=False)
        sql = gr.Code(label="SQL（成功時為實際執行版本；失敗時為最後一版）",
                      language="sql", interactive=False)
        status = gr.Textbox(label="執行狀態及修正次數", interactive=False)
        button.click(submit, inputs=question, outputs=[answer, sql, status],
                     concurrency_limit=1)
    demo.queue()
    return demo


if __name__ == "__main__":
    create_app().launch(server_name="127.0.0.1", share=False, show_error=False)
