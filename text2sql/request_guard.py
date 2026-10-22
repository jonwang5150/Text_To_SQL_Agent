"""Reject explicit database-write requests before asking the SQL model."""

import re
import unicodedata

from sqlglot import exp, parse
from sqlglot.errors import ErrorLevel, SqlglotError


WRITE_REQUEST_MESSAGE = (
    "已阻擋資料異動要求：此查詢器僅支援唯讀查詢，不能新增、刪除、修改資料"
    "或變更資料表結構。本次未執行 SQL。"
)

_WRITE_ACTION = re.compile(
    r"刪除|删除|刪掉|删掉|刪去|删去|刪光|删光|移除|清空|清除|抹除|清掉|"
    r"新增|插入|寫入|写入|匯入|汇入|導入|导入|補入|补入|補上|补上|"
    r"修改|更新|更改|變更|变更|改成|改為|改为|改名|重新命名|重命名|"
    r"設為|设为|設成|设成|換成|换成|調整[為到成]|调整[为到成]|"
    r"覆寫|覆写|覆蓋|覆盖|替換|替换|取代|"
    r"\b(?:insert|update|delete|truncate|remove|erase|purge|wipe|append|"
    r"modify|edit|rename|replace|upsert|merge|alter|drop)\b",
    re.IGNORECASE,
)
_CONTEXTUAL_ACTION = re.compile(
    r"建立|建置|創建|创建|新建|建表|建(?:一)?[張张個个]|增加|加入|添加|加上|設定|设置|"
    r"\b(?:add|create|change|set)\b", re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"(?:把|將|将)[^。；;，,\n!?！？]{1,160}?"
    r"(?:加上|減去|减去|加|減|减|乘以|除以|調高|调高|調低|调低|提高|降低)"
    r"\s*[-+]?\d",
)
_NEGATED_OR_PAST = re.compile(
    r"(?:不要|不需要|不想|不必|不用|請勿|请勿|無需|无需|不能|不可|不准|禁止|"
    r"別|别|勿|不|未|已|已經|已经|被|沒有|没有|未曾|不曾|"
    r"\bnot|\bdon't|\bnever|\bwithout)\s*$",
    re.IGNORECASE,
)
_IMPERATIVE_TAIL = re.compile(
    r"(?:請|请|請你|请你|幫我|帮我|替我|麻煩|麻烦|我要|我想|"
    r"並|并|再|然後|然后|接著|接着|順便|顺便|直接|先|"
    r"\bplease|\band|\bthen)\s*$", re.IGNORECASE,
)
_READ_CONTEXT = re.compile(
    r"查詢|查询|顯示|显示|列出|統計|统计|計算|计算|檢視|检视|查看|搜尋|搜索|"
    r"\b(?:show|list|count|find|display|calculate|what|how\s+many)\b", re.IGNORECASE,
)
_DESCRIPTION_HEAD = re.compile(
    r"(?:的|過的|过的|後的|后的|紀錄|記錄|记录|歷史|历史|時間|时间|日期|"
    r"狀態|状态|次數|次数|數量|数量|\b(?:logs?|history|time|date|status|count)\b)",
    re.IGNORECASE,
)
_RECENT_TAIL = re.compile(
    r"(?:最近|最新|最後|最后|上次|本月|每月|每天|每年|今日|今天|昨天|今年|"
    r"本週|本周|\d{4}年)\s*$",
)
_DATABASE_OBJECT = re.compile(
    r"資料|数据|紀錄|記錄|记录|表格|資料表|数据表|新表|欄位|字段|結構|结构|"
    r"\b(?:tables?|rows?|records?|data|columns?|schema|database)\b", re.IGNORECASE,
)
_SQL_WRITE_STARTS = {
    "insert", "update", "delete", "truncate", "create", "alter", "drop", "merge",
    "replace", "upsert", "rename", "copy", "grant", "revoke", "comment",
}
_SQL_WRITE_NODES = _SQL_WRITE_STARTS | {"truncatetable", "into"}
_CLAUSE_BREAK = re.compile(r"[。；;，,\n!?！？]")


def _sql_write_request(question: str) -> bool | None:
    """Recognize raw SQL, including writes nested in a SELECT/WITH or SQL batch."""
    source = question
    while True:
        stripped = re.sub(r"^\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/)", "", source, count=1, flags=re.DOTALL)
        if stripped == source:
            break
        source = stripped
    first = re.match(r"\s*([a-z]+)\b", source, re.IGNORECASE)
    keyword = first.group(1).lower() if first else ""
    if keyword in _SQL_WRITE_STARTS:
        return True
    if keyword not in {"select", "with"}:
        return None
    try:
        trees = [tree for tree in parse(source, read="postgres", error_level=ErrorLevel.RAISE) if tree]
    except SqlglotError:
        return None
    if any(node.key in _SQL_WRITE_NODES for tree in trees for node in tree.walk()):
        return True
    if trees and all(isinstance(tree, exp.Query) for tree in trees):
        return False
    return None


def _readonly_context(question: str, action: re.Match) -> bool:
    before = _CLAUSE_BREAK.split(question[:action.start()])[-1].rstrip()
    after = question[action.end():].lstrip()
    if _NEGATED_OR_PAST.search(before):
        return True
    if _IMPERATIVE_TAIL.search(before):
        return False
    return bool(_READ_CONTEXT.search(before) and (
        _DESCRIPTION_HEAD.match(after) or _RECENT_TAIL.search(before)
        or (action.group().lower() in {"增加", "加上", "add"} and re.match(r"[-+]?\d", after))
    ))


def is_write_request(question: str) -> bool:
    """Conservative keyword gate; AST validation and DB permissions still apply."""
    normalized = unicodedata.normalize("NFKC", question)
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Cf")
    normalized = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", normalized)
    sql_result = _sql_write_request(normalized)
    if sql_result is not None:
        return sql_result
    for pattern in (_WRITE_ACTION, _ASSIGNMENT, _CONTEXTUAL_ACTION):
        for action in pattern.finditer(normalized):
            if pattern is _CONTEXTUAL_ACTION:
                before = _CLAUSE_BREAK.split(normalized[:action.start()])[-1]
                after = _CLAUSE_BREAK.split(normalized[action.end():], maxsplit=1)[0]
                clause = before + action.group() + after
                if before.strip() and not (_DATABASE_OBJECT.search(clause) or _IMPERATIVE_TAIL.search(before)):
                    continue
            if not _readonly_context(normalized, action):
                return True
    return False
