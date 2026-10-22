"""Shared state and explicit, bounded LangGraph routing."""

from typing import Any, Callable, Literal, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from database import QueryResult


# 定義各流程節點共用的問題、SQL、結果與錯誤狀態。
class AgentState(TypedDict):
    question: str
    schema: str
    sql_query: str
    query_result: QueryResult | None
    error_message: str | None
    error_kind: str | None
    retry_count: int
    final_answer: str
    status: Literal["running", "repairing", "success", "failed"]


MAX_REPAIRS = 3
GRAPH_RECURSION_LIMIT = 32


# 為每次提問建立獨立狀態，避免沿用先前結果或修正次數。
def initial_state(question: str) -> AgentState:
    return {
        "question": question.strip(), "schema": "", "sql_query": "",
        "query_result": None, "error_message": None, "error_kind": None,
        "retry_count": 0, "final_answer": "", "status": "running",
    }


# 組裝查詢節點與分支路徑，限制 SQL 自動修正次數。
def build_graph(
    llm: BaseChatModel, cached_schema: str, allowed_tables: tuple[str, ...],
    execute: Callable[[str], QueryResult], max_result_rows: int = 100,
    startup_error: str | None = None,
) -> Any:
    # Local import avoids a runtime cycle: nodes only imports State for type checking.
    from nodes import make_nodes

    nodes = make_nodes(llm, cached_schema, allowed_tables, execute,
                       max_result_rows, startup_error)
    graph = StateGraph(AgentState)
    for name, node in nodes.items():
        graph.add_node(name, node)

    # Schema 載入成功後產生 SQL，失敗則直接整理錯誤回覆。
    def after_schema(state: AgentState) -> str:
        return "answer_generator" if state["error_message"] else "sql_generator"

    # 模型成功回傳 SQL 後進行驗證，失敗則結束查詢。
    def after_generation(state: AgentState) -> str:
        return "answer_generator" if state["error_message"] else "sql_validator"

    # 依驗證或執行結果，決定執行 SQL、嘗試修正或產生回覆。
    def after_attempt(state: AgentState) -> str:
        if not state["error_message"]:
            return "answer_generator" if state["query_result"] is not None else "sql_executor"
        if state["error_kind"] in {"validation", "query"} and state["retry_count"] < MAX_REPAIRS:
            return "sql_repair"
        return "answer_generator"

    graph.add_edge(START, "schema_loader")
    graph.add_conditional_edges("schema_loader", after_schema,
                                ["sql_generator", "answer_generator"])
    graph.add_conditional_edges("sql_generator", after_generation,
                                ["sql_validator", "answer_generator"])
    for name in ("sql_validator", "sql_executor"):
        graph.add_conditional_edges(name, after_attempt,
                                    ["sql_executor", "sql_repair", "answer_generator"])
    graph.add_conditional_edges("sql_repair", after_generation,
                                ["sql_validator", "answer_generator"])
    graph.add_edge("answer_generator", END)
    return graph.compile()
