"""Reject explicit deletion requests before asking the SQL model."""

import re
import unicodedata


DELETE_REQUEST_MESSAGE = (
    "已阻擋刪除要求：此查詢器僅支援唯讀查詢，不能刪除資料或資料表。本次未執行 SQL。"
)

_DELETE_ACTION = re.compile(
    r"刪除|删除|刪掉|删掉|刪去|删去|刪光|删光|移除|清空|清除|抹除|清掉|"
    r"\b(?:delete|truncate|remove|erase|purge|wipe)\b|"
    r"\bdrop\s+(?:table|database|schema)\b",
    re.IGNORECASE,
)
_READ_ONLY_CONTEXT = re.compile(
    r"(?:不要|不需要|不必|不用|請勿|请勿|無需|无需|不能|不可|不准|禁止|"
    r"別|别|勿|不|未|已|已經|已经|被|沒有|没有|未曾|不曾|"
    r"\bnot|\bdon't|\bnever|\bwithout)\s*$",
    re.IGNORECASE,
)


def is_delete_request(question: str) -> bool:
    """Conservative keyword gate; AST validation and DB permissions still apply."""
    normalized = unicodedata.normalize("NFKC", question)
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Cf")
    normalized = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", normalized)
    for action in _DELETE_ACTION.finditer(normalized):
        # Permit read-only questions about already-deleted or non-deleted records,
        # and explicit negations such as "不要刪除，請顯示前五筆".
        if not _READ_ONLY_CONTEXT.search(normalized[:action.start()]):
            return True
    return False
