// Gradio HTML's mount hook. Delegate clicks so filtered rows keep working.
let resetCopyTimer;

async function copyTableName(text) {
    try {
        await navigator.clipboard.writeText(text);
        return;
    } catch {
        // Support browsers where the Clipboard API is unavailable or denied.
        const previousFocus = document.activeElement;
        const temporary = document.createElement("textarea");
        temporary.value = text;
        temporary.readOnly = true;
        temporary.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
        document.body.appendChild(temporary);
        try {
            temporary.select();
            if (!document.execCommand("copy")) {
                throw new Error("Clipboard copy failed");
            }
        } finally {
            temporary.remove();
            previousFocus?.focus({ preventScroll: true });
        }
    }
}

function resetCopyButtons() {
    element.querySelectorAll(".table-copy[data-copied]").forEach((button) => {
        delete button.dataset.copied;
        button.title = `複製 ${button.dataset.copyTable}`;
        button.setAttribute("aria-label", `複製資料表名稱 ${button.dataset.copyTable}`);
    });
}

element.addEventListener("click", async (event) => {
    const button = event.target instanceof Element
        ? event.target.closest("button[data-copy-table]")
        : null;
    if (!button || !element.contains(button) || button.getAttribute("aria-busy") === "true") return;
    event.preventDefault();
    event.stopPropagation();
    button.setAttribute("aria-busy", "true");
    clearTimeout(resetCopyTimer);
    resetCopyButtons();
    try {
        await copyTableName(button.dataset.copyTable);
        button.dataset.copied = "true";
        button.title = "已複製";
        button.setAttribute("aria-label", `已複製 ${button.dataset.copyTable}`);
        const status = element.querySelector(".table-copy-status");
        if (status) status.textContent = `已複製 ${button.dataset.copyTable}`;
        resetCopyTimer = setTimeout(() => {
            resetCopyButtons();
            if (status?.isConnected) status.textContent = "";
        }, 2200);
    } catch {
        const status = element.querySelector(".table-copy-status");
        if (status) status.textContent = "無法複製，請選取資料表名稱後手動複製。";
    } finally {
        button.removeAttribute("aria-busy");
    }
});
