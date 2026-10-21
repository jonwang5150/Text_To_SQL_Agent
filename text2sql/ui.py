"""Presentation for the local Gradio workspace; no external assets required."""

from html import escape
from pathlib import Path

import gradio as gr


APP_CSS = Path(__file__).with_name("ui.css").read_text(encoding="utf-8")
TABLE_COPY_JS = Path(__file__).with_name("ui.js").read_text(encoding="utf-8")
APP_THEME = gr.themes.Soft(
    primary_hue="indigo", secondary_hue="slate", neutral_hue="slate",
    font=["Segoe UI", "Microsoft JhengHei", "sans-serif"],
    font_mono=["Cascadia Code", "Consolas", "monospace"],
    radius_size="lg",
).set(
    body_text_size="16px", input_text_size="16px", prose_text_size="15px",
    block_label_text_size="14px", block_title_text_size="15px",
    block_info_text_size="14px", button_medium_text_size="16px",
    body_background_fill="#f4f6fa", body_background_fill_dark="#0d1422",
    body_text_color="#24324b", body_text_color_dark="#dce5f4",
    body_text_color_subdued="#64748b", body_text_color_subdued_dark="#a1aec3",
    block_background_fill="#ffffff", block_background_fill_dark="#151f30",
    block_border_color="#e4e9f2", block_border_color_dark="#2b374d",
    block_shadow="none", block_shadow_dark="none",
    input_background_fill="#f8faff", input_background_fill_dark="#101a2a",
    input_border_color="#dde4f0", input_border_color_dark="#35425b",
    input_border_color_focus="#6366f1", input_border_color_focus_dark="#818cf8",
    input_shadow_focus="0 0 0 3px #6366f11a",
    input_shadow_focus_dark="0 0 0 3px #818cf826",
    button_primary_background_fill="#4f46e5", button_primary_background_fill_dark="#6366f1",
    button_primary_background_fill_hover="#4338ca",
    button_primary_background_fill_hover_dark="#818cf8",
    button_primary_border_color="#4f46e5", button_primary_border_color_dark="#6366f1",
    button_primary_text_color="#ffffff", button_primary_text_color_dark="#ffffff",
    button_primary_shadow="0 4px 12px #4f46e526",
    button_primary_shadow_dark="0 4px 12px #00000033",
    button_medium_radius="10px", button_medium_text_weight="600",
)

DATABASE_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<ellipse cx="12" cy="5" rx="8" ry="3"/>'
    '<path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>'
    '<path d="M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></svg>'
)

COPY_ICON = (
    '<svg class="copy-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="8" y="8" width="12" height="12" rx="2"/>'
    '<path d="M16 8V4a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h4"/></svg>'
    '<svg class="check-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="m5 12 4 4 10-10"/></svg>'
)


def header_html(ready: bool) -> str:
    state_class, state_label = ("ready", "可開始查詢") if ready else ("pending", "待完成設定")
    return f"""
    <header class="workspace-header">
      <div class="brand"><span class="brand-mark">{DATABASE_ICON}</span>
        <div><strong>Mini Text-to-SQL</strong><span class="brand-caption">資料查詢工作台</span></div>
      </div>
      <div class="workspace-state {state_class}" role="status">
        <span class="state-dot" aria-hidden="true"></span>{state_label}
      </div>
    </header>
    """


def hero_html(table_count: int, max_rows: int) -> str:
    return f"""
    <section class="workspace-hero" aria-labelledby="workspace-title">
      <div class="hero-copy"><span class="eyebrow">DATA QUERY WORKSPACE</span>
        <h1 id="workspace-title">資料查詢工作台</h1>
        <p>從資料庫中查找答案，輕鬆篩選、排序與統計。</p>
      </div>
      <div class="workspace-facts" aria-label="查詢範圍">
        <div><strong>{table_count:02d}</strong><span>已設定資料表</span></div>
        <div><strong>{max_rows}<small> 筆</small></strong><span>單次結果上限</span></div>
        <span class="readonly-badge">唯讀模式</span>
      </div>
    </section>
    """


def section_html(number: str, title: str, description: str) -> str:
    return (
        '<div class="section-heading">'
        f'<span class="section-number">{escape(number)}</span>'
        f'<div><h2>{escape(title)}</h2><p>{escape(description)}</p></div></div>'
    )


def scope_header_html(table_count: int) -> str:
    return (
        '<div class="scope-heading"><h2 id="scope-title">資料範圍</h2>'
        f'<span class="scope-count">{table_count}</span></div>'
        '<p class="scope-caption">可查詢的資料表</p>'
    )


def tables_html(tables: tuple[str, ...], search: str = "") -> str:
    """Filter configured table names without fetching schema or database data."""
    query = search.strip().casefold()
    matches = [table for table in tables if query in table.casefold()]
    if not matches:
        message = "找不到符合的資料表。" if tables else "完成設定後，這裡會顯示可查詢的資料表。"
        return f'<p class="scope-empty">{message}</p>'
    items = "".join(
        '<li class="table-card">'
        f'<span class="table-icon">{DATABASE_ICON}</span>'
        f'<code>{escape(table)}</code>'
        f'<button type="button" class="table-copy" data-copy-table="{escape(table)}" '
        f'title="複製 {escape(table)}" aria-label="複製資料表名稱 {escape(table)}">'
        f'{COPY_ICON}</button></li>'
        for table in matches
    )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="可查詢資料表清單">'
        f'<ul class="table-list">{items}</ul></div>'
        f'<p class="scope-summary">顯示 {len(matches)} / {len(tables)} 張資料表</p>'
        '<p class="table-copy-status" role="status" aria-live="polite" aria-atomic="true"></p>'
    )
