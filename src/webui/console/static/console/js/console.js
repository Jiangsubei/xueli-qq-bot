const STORAGE_KEYS = {
    theme: "console-theme",
    colors: "console-rgb",
    page: "console-page",
    collapsed: "console-sidebar-collapsed"
};

function readJsonScript(id, fallback = {}) {
    const node = document.getElementById(id);
    if (!node) return fallback;
    try {
        return JSON.parse(node.textContent || "{}");
    } catch {
        return fallback;
    }
}

const CLIENT_CONFIG = readJsonScript("console-client-config", { refreshIntervalMs: 5000, urls: {} });
const RUNTIME_DATA = readJsonScript("console-runtime-data", { runtime: {}, fields: {} });
const floatingTooltip = { node: null };
const avatarCropState = {
    file: null,
    imageLoaded: false,
    dragging: false,
    dragPointerId: null,
    dragStartX: 0,
    dragStartY: 0,
    startOffsetX: 0,
    startOffsetY: 0,
    naturalWidth: 0,
    naturalHeight: 0,
    stageSize: 320,
    scale: 1,
    minScale: 1,
    offsetX: 0,
    offsetY: 0
};
const SECTION_PAGE_MAP = {
    network: "page-network",
    model: "page-model",
    assistant: "page-reply",
    emoji: "page-emoji",
    memory: "page-memory"
};
const dirtyState = {
    baselines: {},
    dirtySections: new Set(),
    modalMode: "restart"
};
const RESTART_MODAL_COPY = {
    restart: {
        title: "重启助手服务",
        desc: "这会重新启动后端助手，当前页面不会关闭。",
        confirm: "现在重启"
    },
    dirty: {
        title: "保存后重启助手",
        desc: "你刚改了设置。先点保存，再重启，新的设置才会生效；如果现在重启，未保存的改动不会带上。",
        confirm: "仍然重启"
    }
};

function getCsrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

async function parseJsonResponse(response) {
    const contentType = String(response.headers.get("content-type") || "").toLowerCase();
    if (contentType.includes("application/json")) {
        return response.json();
    }
    const text = await response.text();
    throw new Error(text || "\u670d\u52a1\u5668\u8fd4\u56de\u4e0d\u5bf9\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5");
}

async function requestJson(url, options = {}) {
    const response = await fetch(url, {
        headers: {
            "X-Requested-With": "XMLHttpRequest",
            ...(options.headers || {})
        },
        ...options
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok || payload.ok === false) {
        const detail = Array.isArray(payload.errors) && payload.errors.length ? `：${payload.errors[0]}` : "";
        throw new Error(`${payload.message || "\u64cd\u4f5c\u5931\u8d25"}${detail}`);
    }
    return payload;
}

function toggleSidebar() {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return;
    sidebar.classList.toggle("collapsed");
    localStorage.setItem(STORAGE_KEYS.collapsed, sidebar.classList.contains("collapsed") ? "1" : "0");
}

function toggleTheme() {
    const html = document.documentElement;
    if (html.getAttribute("data-theme") === "dark") {
        html.removeAttribute("data-theme");
        localStorage.setItem(STORAGE_KEYS.theme, "light");
    } else {
        html.setAttribute("data-theme", "dark");
        localStorage.setItem(STORAGE_KEYS.theme, "dark");
    }
}

function setPrimaryColor(rgb, element) {
    const [r, g, b] = rgb.split(",").map((part) => part.trim());
    const root = document.documentElement;
    root.style.setProperty("--theme-r", r);
    root.style.setProperty("--theme-g", g);
    root.style.setProperty("--theme-b", b);
    document.querySelectorAll(".color-dot").forEach((dot) => dot.classList.remove("active"));
    if (element) element.classList.add("active");
    localStorage.setItem(STORAGE_KEYS.colors, rgb);
}

function setActivePage(targetId, item) {
    document.querySelectorAll(".menu-item").forEach((menuItem) => menuItem.classList.remove("active"));
    if (item) {
        item.classList.add("active");
        const topTitle = document.getElementById("top-title");
        const menuText = item.querySelector(".menu-text");
        if (topTitle && menuText) topTitle.innerText = menuText.innerText;
    }
    document.querySelectorAll(".page-section").forEach((page) => page.classList.remove("active"));
    const page = document.getElementById(targetId);
    if (page) page.classList.add("active");
    if (targetId === "page-recall") loadRecall();
    if (targetId === "page-memory") loadMemoryItems();
    localStorage.setItem(STORAGE_KEYS.page, targetId);
    updateDirtyNotice();
}

function configureRestartModal(mode = "restart") {
    dirtyState.modalMode = mode;
    const copy = RESTART_MODAL_COPY[mode] || RESTART_MODAL_COPY.restart;
    const title = document.getElementById("restartModalTitle");
    const desc = document.getElementById("restartModalDesc");
    const confirm = document.getElementById("restartConfirm");
    if (title) title.textContent = copy.title;
    if (desc) desc.textContent = copy.desc;
    if (confirm) confirm.textContent = copy.confirm;
}

function showRestartModal(mode = "restart") {
    const modal = document.getElementById("restartModal");
    if (!modal) return;
    configureRestartModal(mode);
    modal.style.display = "flex";
    setTimeout(() => modal.classList.add("show"), 10);
}

function hideRestartModal() {
    const modal = document.getElementById("restartModal");
    if (!modal) return;
    modal.classList.remove("show");
    setTimeout(() => { modal.style.display = "none"; }, 300);
}

function showFeedback(message, tone = "success") {
    const box = document.getElementById("saveFeedback");
    if (!box) return;
    box.textContent = message;
    box.classList.remove("hidden", "is-success", "is-error");
    box.classList.add(tone === "error" ? "is-error" : "is-success");
    window.clearTimeout(showFeedback._timer);
    showFeedback._timer = window.setTimeout(() => box.classList.add("hidden"), 3200);
}

function getSectionForPage(targetId) {
    return Object.entries(SECTION_PAGE_MAP).find(([, pageId]) => pageId === targetId)?.[0] || null;
}

function getActiveSection() {
    const activePageId = document.querySelector(".page-section.active")?.id || localStorage.getItem(STORAGE_KEYS.page) || "page-home";
    return getSectionForPage(activePageId);
}

function snapshotSection(section) {
    return JSON.stringify(collectSectionPayload(section));
}

function markSectionClean(section) {
    if (!section) return;
    dirtyState.baselines[section] = snapshotSection(section);
    dirtyState.dirtySections.delete(section);
    updateDirtyNotice();
}

function recomputeSectionDirty(section) {
    if (!section || !(section in dirtyState.baselines)) return;
    const current = snapshotSection(section);
    if (current === dirtyState.baselines[section]) {
        dirtyState.dirtySections.delete(section);
    } else {
        dirtyState.dirtySections.add(section);
    }
    updateDirtyNotice();
}

function scheduleDirtySync(target) {
    const form = target?.closest("[data-form-section]");
    if (!form || target?.closest(".memory-card")) return;
    const section = form.getAttribute("data-form-section");
    if (!section) return;
    window.requestAnimationFrame(() => recomputeSectionDirty(section));
}

function initDirtyState() {
    Object.keys(SECTION_PAGE_MAP).forEach((section) => {
        const form = document.querySelector(`[data-form-section="${section}"]`);
        if (!form) return;
        dirtyState.baselines[section] = snapshotSection(section);
    });
    updateDirtyNotice();
}

function updateDirtyNotice() {
    const notice = document.getElementById("dirtyRestartNotice");
    const saveButton = document.getElementById("dirtySaveButton");
    const activeSection = getActiveSection();
    const shouldShow = Boolean(activeSection && dirtyState.dirtySections.has(activeSection));
    if (notice) notice.classList.toggle("hidden", !shouldShow);
    if (saveButton) saveButton.classList.toggle("hidden", !shouldShow);
}

async function executeRestart() {
    const confirm = document.getElementById("restartConfirm");
    const cancel = document.getElementById("restartCancel");
    const originalText = confirm ? confirm.textContent : "";
    hideRestartModal();
    if (confirm) {
        confirm.disabled = true;
        confirm.textContent = "\u91cd\u542f\u4e2d...";
    }
    if (cancel) cancel.disabled = true;
    try {
        const payload = await requestJson(CLIENT_CONFIG.urls.runtimeRestart, {
            method: "POST",
            headers: {
                "X-CSRFToken": getCsrfToken()
            }
        });
        showFeedback(payload.message || "\u52a9\u624b\u670d\u52a1\u5df2\u91cd\u542f", "success");
        await pollRuntime();
    } catch (error) {
        showFeedback(error.message || "\u91cd\u542f\u5931\u8d25\uff0c\u8bf7\u67e5\u770b\u65e5\u5fd7", "error");
    } finally {
        if (confirm) {
            confirm.disabled = false;
            confirm.textContent = originalText;
        }
        if (cancel) cancel.disabled = false;
    }
}

function bindNavigation() {
    document.querySelectorAll(".menu-item").forEach((item) => {
        item.addEventListener("click", (event) => {
            event.preventDefault();
            setActivePage(item.getAttribute("data-target"), item);
        });
    });
}

function bindTheme() {
    const button = document.getElementById("themeToggle");
    if (button) button.addEventListener("click", toggleTheme);
}

function bindSidebar() {
    const button = document.getElementById("sidebarToggle");
    if (button) button.addEventListener("click", toggleSidebar);
}

function bindColors() {
    document.querySelectorAll(".color-dot").forEach((dot) => dot.addEventListener("click", () => setPrimaryColor(dot.dataset.rgb, dot)));
}

function bindModal() {
    const open = document.getElementById("restartTrigger");
    const dirtyNotice = document.getElementById("dirtyRestartNotice");
    const dirtySave = document.getElementById("dirtySaveButton");
    const cancel = document.getElementById("restartCancel");
    const confirm = document.getElementById("restartConfirm");
    const modal = document.getElementById("restartModal");
    if (open) open.addEventListener("click", () => showRestartModal("restart"));
    if (dirtyNotice) dirtyNotice.addEventListener("click", () => showRestartModal("dirty"));
    if (dirtySave) dirtySave.addEventListener("click", saveActiveDirtySection);
    if (cancel) cancel.addEventListener("click", hideRestartModal);
    if (confirm) confirm.addEventListener("click", executeRestart);
    if (modal) modal.addEventListener("click", (event) => { if (event.target === modal) hideRestartModal(); });
}

function restoreState() {
    const storedTheme = localStorage.getItem(STORAGE_KEYS.theme);
    if (storedTheme === "dark") document.documentElement.setAttribute("data-theme", "dark");
    const storedColor = localStorage.getItem(STORAGE_KEYS.colors);
    if (storedColor) {
        const dot = Array.from(document.querySelectorAll(".color-dot")).find((node) => node.dataset.rgb === storedColor);
        if (dot) setPrimaryColor(storedColor, dot);
    }
    const storedPage = localStorage.getItem(STORAGE_KEYS.page);
    if (storedPage) {
        const item = document.querySelector(`.menu-item[data-target="${storedPage}"]`);
        if (item) setActivePage(storedPage, item);
    }
    if (localStorage.getItem(STORAGE_KEYS.collapsed) === "1" && window.innerWidth > 900) {
        const sidebar = document.getElementById("sidebar");
        if (sidebar) sidebar.classList.add("collapsed");
    }
}

function updateRuntime(data) {
    const fields = data.fields || {};
    Object.entries(fields).forEach(([key, value]) => {
        document.querySelectorAll(`[data-runtime-key="${key}"]`).forEach((node) => { node.textContent = value; });
    });
    const online = Boolean(data.runtime && data.runtime.online);
    const pill = document.querySelector("[data-runtime-online]");
    if (pill) {
        pill.classList.toggle("is-online", online);
        pill.classList.toggle("is-offline", !online);
    }
    const banner = document.querySelector("[data-offline-banner]");
    if (banner) banner.classList.toggle("hidden", online);
}

async function pollRuntime() {
    const url = CLIENT_CONFIG.urls.dashboard;
    if (!url) return;
    try {
        const payload = await requestJson(url);
        updateRuntime(payload);
    } catch {
        updateRuntime({
            runtime: { online: false },
            fields: {
                assistant_name: document.querySelector('[data-runtime-key="assistant_name"]')?.textContent || "--",
                qq_connection: "未连接",
                vision_status: "未知",
                memory_status: "未知",
                messages_received: "--",
                messages_replied: "--",
                message_errors: "--",
                run_status: "未连接",
                connection_status: "断开",
                uptime: "--",
                active_conversations: "--",
                active_tasks: "--",
                emoji_total: document.querySelector('[data-runtime-key="emoji_total"]')?.textContent || "--",
                emoji_pending: document.querySelector('[data-runtime-key="emoji_pending"]')?.textContent || "--",
                memory_reads: document.querySelector('[data-runtime-key="memory_reads"]')?.textContent || "--",
                memory_writes: document.querySelector('[data-runtime-key="memory_writes"]')?.textContent || "--",
                snapshot_label: "--",
                online_label: "未连接"
            }
        });
    }
}

function checkboxValue(formData, name) {
    return formData.get(name) === "on";
}

function createKvRowElement(key = "", value = "", keyPlaceholder = "Key", valuePlaceholder = "Value") {
    const row = document.createElement("div");
    row.className = "editor-row";
    row.dataset.kvRow = "";
    row.innerHTML = `
        <input class="form-control" type="text" value="${escapeHtml(key)}" placeholder="${escapeHtml(keyPlaceholder)}" data-kv-key>
        <input class="form-control" type="text" value="${escapeHtml(value)}" placeholder="${escapeHtml(valuePlaceholder)}" data-kv-value>
        <button class="btn-outline btn-icon-lite" type="button" data-remove-kv-row>
            <i class="fa-solid fa-xmark"></i>
        </button>
    `;
    return row;
}

function createWindowRowElement(start = "", end = "") {
    const row = document.createElement("div");
    row.className = "editor-row window-row";
    row.dataset.windowRow = "";
    row.innerHTML = `
        <input class="form-control" type="time" value="${escapeHtml(start)}" data-window-start>
        <span class="window-separator">-</span>
        <input class="form-control" type="time" value="${escapeHtml(end)}" data-window-end>
        <button class="btn-outline btn-icon-lite" type="button" data-remove-window-row>
            <i class="fa-solid fa-xmark"></i>
        </button>
    `;
    return row;
}

function createTagChipElement(text) {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.dataset.tagItem = "";
    chip.innerHTML = `
        <span class="tag-chip-text">${escapeHtml(text)}</span>
        <button class="tag-chip-move" type="button" data-move-tag="prev" aria-label="上移">
            <i class="fa-solid fa-arrow-left"></i>
        </button>
        <button class="tag-chip-move" type="button" data-move-tag="next" aria-label="下移">
            <i class="fa-solid fa-arrow-right"></i>
        </button>
        <button class="tag-chip-remove" type="button" data-remove-tag aria-label="删除">
            <i class="fa-solid fa-xmark"></i>
        </button>
    `;
    return chip;
}

function collectKvRows(container) {
    return Array.from(container?.querySelectorAll("[data-kv-row]") || []).map((row) => ({
        key: String(row.querySelector("[data-kv-key]")?.value || "").trim(),
        value: String(row.querySelector("[data-kv-value]")?.value || "").trim()
    }));
}

function collectWindowRows(container) {
    return Array.from(container?.querySelectorAll("[data-window-row]") || [])
        .map((row) => ({
            start: String(row.querySelector("[data-window-start]")?.value || "").trim(),
            end: String(row.querySelector("[data-window-end]")?.value || "").trim()
        }))
        .filter((row) => row.start || row.end);
}

function collectTagItems(container) {
    return Array.from(container?.querySelectorAll("[data-tag-item] .tag-chip-text") || [])
        .map((node) => String(node.textContent || "").trim())
        .filter(Boolean);
}

function numberOrZero(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
}

function collectSectionPayload(section) {
    const form = document.querySelector(`[data-form-section="${section}"]`);
    if (!form) return {};
    const formData = new FormData(form);
    if (section === "network") return { ws_url: String(formData.get("ws_url") || "").trim(), http_url: String(formData.get("http_url") || "").trim() };
    if (section === "model") {
        const payload = { ai_service: {}, group_reply_decision: {}, vision_service: {}, memory_rerank: {}, memory_extraction: {} };
        for (const [name, value] of formData.entries()) {
            const [group, field] = String(name).split("__");
            if (payload[group]) payload[group][field] = String(value || "").trim();
        }
        form.querySelectorAll("[data-model-group]").forEach((groupNode) => {
            const group = groupNode.getAttribute("data-model-group");
            if (!group || !payload[group]) return;
            payload[group].extra_params_rows = collectKvRows(groupNode.querySelector('[data-kv-field="extra_params_rows"]'));
            payload[group].extra_headers_rows = collectKvRows(groupNode.querySelector('[data-kv-field="extra_headers_rows"]'));
        });
        return payload;
    }
    if (section === "assistant") {
        return {
            name: String(formData.get("name") || "").trim(),
            alias: String(formData.get("alias") || "").trim(),
            max_context_length: numberOrZero(formData.get("max_context_length")),
            max_message_length: numberOrZero(formData.get("max_message_length")),
            response_timeout: numberOrZero(formData.get("response_timeout")),
            group_strategy: String(formData.get("group_strategy") || "smart"),
            personality: String(formData.get("personality") || ""),
            dialogue_style: String(formData.get("dialogue_style") || ""),
            rate_limit_interval: numberOrZero(formData.get("rate_limit_interval")),
            log_full_prompt: checkboxValue(formData, "log_full_prompt"),
            plan_request_interval: numberOrZero(formData.get("plan_request_interval")),
            plan_request_max_parallel: numberOrZero(formData.get("plan_request_max_parallel")),
            burst_merge_enabled: checkboxValue(formData, "burst_merge_enabled"),
            burst_window_seconds: numberOrZero(formData.get("burst_window_seconds")),
            burst_min_messages: numberOrZero(formData.get("burst_min_messages")),
            burst_max_messages: numberOrZero(formData.get("burst_max_messages")),
            behavior: String(formData.get("behavior") || "")
        };
    }
    if (section === "emoji") {
        return {
            enabled: checkboxValue(formData, "enabled"),
            capture_enabled: checkboxValue(formData, "capture_enabled"),
            classification_enabled: checkboxValue(formData, "classification_enabled"),
            reply_enabled: checkboxValue(formData, "reply_enabled"),
            idle_seconds_before_classify: numberOrZero(formData.get("idle_seconds_before_classify")),
            classification_interval_seconds: numberOrZero(formData.get("classification_interval_seconds")),
            classification_windows: collectWindowRows(form.querySelector("[data-window-list]")),
            emotion_labels: collectTagItems(form.querySelector("[data-tag-list]")),
            reply_cooldown_seconds: numberOrZero(formData.get("reply_cooldown_seconds")),
            storage_path: String(formData.get("storage_path") || "").trim()
        };
    }
    if (section === "memory") {
        return {
            enabled: checkboxValue(formData, "enabled"),
            auto_extract: checkboxValue(formData, "auto_extract"),
            read_scope: String(formData.get("read_scope") || "user"),
            bm25_top_k: numberOrZero(formData.get("bm25_top_k")),
            rerank_top_k: numberOrZero(formData.get("rerank_top_k")),
            extract_every_n_turns: numberOrZero(formData.get("extract_every_n_turns")),
            conversation_save_interval: numberOrZero(formData.get("conversation_save_interval")),
            ordinary_decay_enabled: checkboxValue(formData, "ordinary_decay_enabled"),
            ordinary_half_life_days: numberOrZero(formData.get("ordinary_half_life_days")),
            ordinary_forget_threshold: numberOrZero(formData.get("ordinary_forget_threshold")),
            storage_path: String(formData.get("storage_path") || "").trim()
        };
    }
    return {};
}

function saveActiveDirtySection() {
    const activeSection = getActiveSection();
    if (!activeSection || !dirtyState.dirtySections.has(activeSection)) return;
    saveSection(activeSection, document.getElementById("dirtySaveButton"));
}

async function saveSection(section, buttonOverride = null) {
    const urlMap = {
        network: CLIENT_CONFIG.urls.networkSave,
        model: CLIENT_CONFIG.urls.modelSave,
        assistant: CLIENT_CONFIG.urls.assistantSave,
        emoji: CLIENT_CONFIG.urls.emojiSave,
        memory: CLIENT_CONFIG.urls.memorySave
    };
    const url = urlMap[section];
    if (!url) return;
    const button = buttonOverride || document.querySelector(`[data-save-action="${section}"]`);
    const originalText = button ? button.textContent : "";
    if (button) {
        button.disabled = true;
        button.textContent = "保存中...";
    }
    try {
        const payload = await requestJson(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken()
            },
            body: JSON.stringify(collectSectionPayload(section))
        });
        markSectionClean(section);
        showFeedback(payload.message || "\u5df2\u7ecf\u8bb0\u597d\u4e86\uff0c\u91cd\u542f\u52a9\u624b\u540e\u751f\u6548", "success");
    } catch (error) {
        showFeedback(error.message || "\u6ca1\u4fdd\u5b58\u6210\u529f\uff0c\u8bf7\u518d\u8bd5\u4e00\u6b21", "error");
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = originalText;
        }
    }
}

function bindSaveActions() {
    document.querySelectorAll("[data-save-action]").forEach((button) => button.addEventListener("click", () => saveSection(button.dataset.saveAction, button)));
}

function ensureEditorNotEmpty(list, factory) {
    if (!list || list.children.length) return;
    list.appendChild(factory());
}

function bindDirtyTracking() {
    document.addEventListener("input", (event) => scheduleDirtySync(event.target));
    document.addEventListener("change", (event) => scheduleDirtySync(event.target));
    document.addEventListener("click", (event) => {
        if (event.target.closest("[data-add-kv-row], [data-remove-kv-row], [data-add-window-row], [data-remove-window-row], [data-add-tag], [data-remove-tag], [data-move-tag], .strategy-option")) {
            scheduleDirtySync(event.target);
        }
    });
}

function bindVisualEditors() {
    document.addEventListener("click", (event) => {
        const addKv = event.target.closest("[data-add-kv-row]");
        if (addKv) {
            event.preventDefault();
            const panel = addKv.closest("[data-kv-field]");
            const list = panel?.querySelector("[data-kv-list]");
            if (list) list.appendChild(createKvRowElement());
            return;
        }

        const removeKv = event.target.closest("[data-remove-kv-row]");
        if (removeKv) {
            event.preventDefault();
            const row = removeKv.closest("[data-kv-row]");
            const list = row?.parentElement;
            row?.remove();
            ensureEditorNotEmpty(list, () => createKvRowElement());
            return;
        }

        const addWindow = event.target.closest("[data-add-window-row]");
        if (addWindow) {
            event.preventDefault();
            const panel = addWindow.closest(".editor-panel");
            const list = panel?.querySelector("[data-window-list]");
            if (list) list.appendChild(createWindowRowElement());
            return;
        }

        const removeWindow = event.target.closest("[data-remove-window-row]");
        if (removeWindow) {
            event.preventDefault();
            const row = removeWindow.closest("[data-window-row]");
            const list = row?.parentElement;
            row?.remove();
            ensureEditorNotEmpty(list, () => createWindowRowElement());
            return;
        }

        const addTag = event.target.closest("[data-add-tag]");
        if (addTag) {
            event.preventDefault();
            const editor = addTag.closest("[data-tag-editor]");
            const input = editor?.querySelector("[data-tag-input]");
            const list = editor?.querySelector("[data-tag-list]");
            const value = String(input?.value || "").trim();
            if (!value || !list) return;
            list.appendChild(createTagChipElement(value));
            input.value = "";
            input.focus();
            return;
        }

        const removeTag = event.target.closest("[data-remove-tag]");
        if (removeTag) {
            event.preventDefault();
            removeTag.closest("[data-tag-item]")?.remove();
            return;
        }

        const moveTag = event.target.closest("[data-move-tag]");
        if (moveTag) {
            event.preventDefault();
            const direction = moveTag.getAttribute("data-move-tag");
            const tag = moveTag.closest("[data-tag-item]");
            const list = tag?.parentElement;
            if (!tag || !list) return;
            if (direction === "prev" && tag.previousElementSibling) {
                list.insertBefore(tag, tag.previousElementSibling);
            }
            if (direction === "next" && tag.nextElementSibling) {
                list.insertBefore(tag.nextElementSibling, tag);
            }
        }
    });

    document.addEventListener("keydown", (event) => {
        const input = event.target.closest("[data-tag-input]");
        if (!input || event.key !== "Enter") return;
        event.preventDefault();
        input.closest("[data-tag-editor]")?.querySelector("[data-add-tag]")?.click();
    });

    document.querySelectorAll("[data-kv-list]").forEach((list) => ensureEditorNotEmpty(list, () => createKvRowElement()));
    document.querySelectorAll("[data-window-list]").forEach((list) => ensureEditorNotEmpty(list, () => createWindowRowElement()));
}
function bindTooltipLayer() {
    floatingTooltip.node = document.getElementById("floatingTooltip");
    if (!floatingTooltip.node) return;
    document.addEventListener("mouseover", (event) => {
        const trigger = event.target.closest("[data-help]");
        if (!trigger) return;
        const text = trigger.getAttribute("data-help");
        if (!text) return;
        floatingTooltip.node.textContent = text;
        floatingTooltip.node.classList.remove("hidden");
        positionTooltip(trigger);
    });
    document.addEventListener("mousemove", (event) => {
        const trigger = event.target.closest("[data-help]");
        if (!trigger || !floatingTooltip.node || floatingTooltip.node.classList.contains("hidden")) return;
        positionTooltip(trigger, event.clientX, event.clientY);
    });
    document.addEventListener("mouseout", (event) => {
        if (!event.target.closest("[data-help]") || !floatingTooltip.node) return;
        floatingTooltip.node.classList.add("hidden");
    });
}

function positionTooltip(trigger, clientX, clientY) {
    if (!floatingTooltip.node) return;
    const rect = trigger.getBoundingClientRect();
    const x = clientX ?? (rect.left + rect.width / 2);
    const y = clientY ?? rect.top;
    floatingTooltip.node.style.left = `${Math.min(window.innerWidth - 260, Math.max(16, x + 16))}px`;
    floatingTooltip.node.style.top = `${Math.max(16, y - 12)}px`;
}

function bindOptionPicker({ triggerId, cardId, inputId, labelId, optionSelector, valueKey, labelKey, selectedClass = "is-selected" }) {
    const trigger = document.getElementById(triggerId);
    const card = document.getElementById(cardId);
    const input = document.getElementById(inputId);
    const label = document.getElementById(labelId);
    if (!trigger || !card || !input || !label) return;

    const field = trigger.closest(".strategy-picker-field");
    const sectionCard = trigger.closest(".config-section-card");

    const closeCard = () => {
        card.classList.add("hidden");
        trigger.setAttribute("aria-expanded", "false");
        field?.classList.remove("is-open");
        sectionCard?.classList.remove("is-picker-open");
    };

    trigger.addEventListener("click", (event) => {
        event.preventDefault();
        const opening = card.classList.contains("hidden");
        document.querySelectorAll(".strategy-card").forEach((node) => {
            if (node !== card) node.classList.add("hidden");
        });
        if (!opening) {
            closeCard();
            return;
        }
        card.classList.remove("hidden");
        trigger.setAttribute("aria-expanded", "true");
        field?.classList.add("is-open");
        sectionCard?.classList.add("is-picker-open");
    });

    card.querySelectorAll(optionSelector).forEach((option) => {
        option.addEventListener("click", () => {
            const value = option.dataset[valueKey] || "";
            const text = option.dataset[labelKey] || "";
            input.value = value;
            label.textContent = text;
            card.querySelectorAll(optionSelector).forEach((node) => node.classList.remove(selectedClass));
            option.classList.add(selectedClass);
            closeCard();
            scheduleDirtySync(input);
        });
    });

    document.addEventListener("click", (event) => {
        if (!card.classList.contains("hidden") && !card.contains(event.target) && !trigger.contains(event.target)) {
            closeCard();
        }
    });
}

function updateAvatar(url) {
    const image = document.getElementById("assistantAvatarImage");
    const fallback = document.getElementById("assistantAvatarFallback");
    if (!image || !fallback) return;
    if (url) {
        image.src = url;
        image.classList.remove("hidden");
        fallback.classList.add("hidden");
    } else {
        image.removeAttribute("src");
        image.classList.add("hidden");
        fallback.classList.remove("hidden");
    }
}

function resetAvatarCropState() {
    Object.assign(avatarCropState, {
        file: null,
        imageLoaded: false,
        dragging: false,
        dragPointerId: null,
        dragStartX: 0,
        dragStartY: 0,
        startOffsetX: 0,
        startOffsetY: 0,
        naturalWidth: 0,
        naturalHeight: 0,
        stageSize: 320,
        scale: 1,
        minScale: 1,
        offsetX: 0,
        offsetY: 0
    });
}

function syncCropImageTransform() {
    const image = document.getElementById("avatarCropImage");
    if (!image || !avatarCropState.imageLoaded) return;
    image.style.transform = `translate(${avatarCropState.offsetX}px, ${avatarCropState.offsetY}px) scale(${avatarCropState.scale})`;
}

function clampCropOffset() {
    const image = document.getElementById("avatarCropImage");
    if (!image || !avatarCropState.imageLoaded) return;
    const stage = avatarCropState.stageSize;
    const scaledWidth = avatarCropState.naturalWidth * avatarCropState.scale;
    const scaledHeight = avatarCropState.naturalHeight * avatarCropState.scale;
    const minX = Math.min(0, stage - scaledWidth);
    const minY = Math.min(0, stage - scaledHeight);
    avatarCropState.offsetX = Math.max(minX, Math.min(0, avatarCropState.offsetX));
    avatarCropState.offsetY = Math.max(minY, Math.min(0, avatarCropState.offsetY));
    syncCropImageTransform();
}

function openAvatarCropModal(dataUrl, file) {
    const modal = document.getElementById("avatarCropModal");
    const image = document.getElementById("avatarCropImage");
    const stage = document.getElementById("avatarCropStage");
    if (!modal || !image || !stage) return;
    resetAvatarCropState();
    avatarCropState.file = file;
    avatarCropState.stageSize = Math.round(stage.getBoundingClientRect().width || 320);
    image.onload = () => {
        avatarCropState.naturalWidth = image.naturalWidth;
        avatarCropState.naturalHeight = image.naturalHeight;
        avatarCropState.minScale = Math.max(
            avatarCropState.stageSize / avatarCropState.naturalWidth,
            avatarCropState.stageSize / avatarCropState.naturalHeight
        );
        avatarCropState.scale = avatarCropState.minScale;
        avatarCropState.offsetX = (avatarCropState.stageSize - avatarCropState.naturalWidth * avatarCropState.scale) / 2;
        avatarCropState.offsetY = (avatarCropState.stageSize - avatarCropState.naturalHeight * avatarCropState.scale) / 2;
        avatarCropState.imageLoaded = true;
        syncCropImageTransform();
        modal.style.display = "flex";
        requestAnimationFrame(() => modal.classList.add("show"));
    };
    image.src = dataUrl;
}

function closeAvatarCropModal() {
    const modal = document.getElementById("avatarCropModal");
    if (!modal) return;
    modal.classList.remove("show");
    setTimeout(() => { modal.style.display = "none"; }, 240);
    resetAvatarCropState();
}

function bindAvatarCropInteractions() {
    const stage = document.getElementById("avatarCropStage");
    const cancel = document.getElementById("avatarCropCancel");
    const confirm = document.getElementById("avatarCropConfirm");
    const modal = document.getElementById("avatarCropModal");
    if (!stage || !cancel || !confirm || !modal) return;

    stage.addEventListener("pointerdown", (event) => {
        if (!avatarCropState.imageLoaded) return;
        avatarCropState.dragging = true;
        avatarCropState.dragPointerId = event.pointerId;
        avatarCropState.dragStartX = event.clientX;
        avatarCropState.dragStartY = event.clientY;
        avatarCropState.startOffsetX = avatarCropState.offsetX;
        avatarCropState.startOffsetY = avatarCropState.offsetY;
        stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener("pointermove", (event) => {
        if (!avatarCropState.dragging || event.pointerId !== avatarCropState.dragPointerId) return;
        avatarCropState.offsetX = avatarCropState.startOffsetX + (event.clientX - avatarCropState.dragStartX);
        avatarCropState.offsetY = avatarCropState.startOffsetY + (event.clientY - avatarCropState.dragStartY);
        clampCropOffset();
    });
    const stopDrag = (event) => {
        if (avatarCropState.dragPointerId !== event.pointerId) return;
        avatarCropState.dragging = false;
        stage.releasePointerCapture(event.pointerId);
    };
    stage.addEventListener("pointerup", stopDrag);
    stage.addEventListener("pointercancel", stopDrag);
    stage.addEventListener("wheel", (event) => {
        if (!avatarCropState.imageLoaded) return;
        event.preventDefault();
        const delta = event.deltaY < 0 ? 1.08 : 0.92;
        avatarCropState.scale = Math.max(avatarCropState.minScale, Math.min(avatarCropState.minScale * 3, avatarCropState.scale * delta));
        clampCropOffset();
    }, { passive: false });

    cancel.addEventListener("click", closeAvatarCropModal);
    confirm.addEventListener("click", uploadCroppedAvatar);
    modal.addEventListener("click", (event) => { if (event.target === modal) closeAvatarCropModal(); });
}

async function uploadCroppedAvatar() {
    const trigger = document.getElementById("assistantAvatarTrigger");
    const confirm = document.getElementById("avatarCropConfirm");
    const canvas = document.createElement("canvas");
    const image = document.getElementById("avatarCropImage");
    const size = 512;
    canvas.width = size;
    canvas.height = size;
    const context = canvas.getContext("2d");
    const scaleRatio = size / avatarCropState.stageSize;
    context.drawImage(
        image,
        avatarCropState.offsetX * scaleRatio,
        avatarCropState.offsetY * scaleRatio,
        avatarCropState.naturalWidth * avatarCropState.scale * scaleRatio,
        avatarCropState.naturalHeight * avatarCropState.scale * scaleRatio
    );
    confirm.disabled = true;
    if (trigger) trigger.disabled = true;
    try {
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png", 1));
        if (!blob) throw new Error("头像裁切失败，请重试");
        const file = new File([blob], "avatar.png", { type: "image/png" });
        const formData = new FormData();
        formData.append("avatar", file);
        const payload = await requestJson(CLIENT_CONFIG.urls.avatarUpload, {
            method: "POST",
            headers: {
                "X-CSRFToken": getCsrfToken()
            },
            body: formData
        });
        updateAvatar(payload.avatar_url || "");
        closeAvatarCropModal();
        showFeedback(payload.message || "\u5934\u50cf\u5df2\u7ecf\u6362\u597d\u4e86", "success");
    } catch (error) {
        showFeedback(error.message || "\u5934\u50cf\u4e0a\u4f20\u5931\u8d25\uff0c\u8bf7\u518d\u8bd5\u4e00\u6b21", "error");
    } finally {
        confirm.disabled = false;
        if (trigger) trigger.disabled = false;
        const input = document.getElementById("assistantAvatarInput");
        if (input) input.value = "";
    }
}

function bindAvatarUpload() {
    const trigger = document.getElementById("assistantAvatarTrigger");
    const input = document.getElementById("assistantAvatarInput");
    if (!trigger || !input || !CLIENT_CONFIG.urls.avatarUpload) return;
    trigger.addEventListener("click", () => input.click());
    input.addEventListener("change", async () => {
        const file = input.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => openAvatarCropModal(String(reader.result || ""), file);
        reader.readAsDataURL(file);
    });
}

function renderMemorySections(sections) {
    const board = document.getElementById("memoryBoard");
    if (!board) return;
    board.innerHTML = (sections || []).map((section) => {
        const items = Array.isArray(section.items) ? section.items : [];
        const cards = items.map((item) => `
            <article class="memory-card" data-memory-id="${escapeHtml(item.id)}" data-memory-kind="${escapeHtml(item.kind)}" data-memory-owner="${escapeHtml(item.owner_user_id || "")}">
                <div class="memory-card-head">
                    <div class="memory-card-meta-row">
                        <span class="memory-chip ${item.is_shared ? "is-shared" : "is-private"}">${item.is_shared ? "共享" : "私有"}</span>
                        ${item.owner_user_id ? `<span class="memory-chip">用户 ${escapeHtml(item.owner_user_id)}</span>` : ""}
                        ${item.group_id ? `<span class="memory-chip">群 ${escapeHtml(item.group_id)}</span>` : ""}
                    </div>
                    <span class="memory-time">${escapeHtml(item.updated_at || item.created_at || "")}</span>
                </div>
                <div class="memory-card-body">
                    <p class="memory-card-text" data-memory-text>${escapeHtml(item.content)}</p>
                    <textarea class="form-control form-textarea memory-editor hidden" data-memory-editor>${escapeHtml(item.content)}</textarea>
                </div>
                <div class="memory-card-actions">
                    <button class="btn-outline memory-action" type="button" data-memory-edit>编辑</button>
                    <button class="btn-outline memory-action hidden" type="button" data-memory-save>保存</button>
                    <button class="btn-outline btn-quiet memory-action hidden" type="button" data-memory-cancel>取消</button>
                    <button class="btn-outline btn-danger-lite memory-action" type="button" data-memory-delete>删除</button>
                </div>
            </article>
        `).join("");
        return `
            <section class="memory-section-block" data-memory-section="${escapeHtml(section.key)}">
                <div class="memory-section-head">
                    <h4>${escapeHtml(section.title)}</h4>
                    <span class="memory-section-count">${items.length}</span>
                </div>
                ${items.length ? `<div class="memory-card-grid ${section.key === "important" ? "memory-card-grid-important" : ""}">${cards}</div>` : `<div class="memory-empty">${escapeHtml(section.empty || "暂无数据")}</div>`}
            </section>
        `;
    }).join("");
}

async function loadMemoryItems() {
    const url = CLIENT_CONFIG.urls.memoryItems;
    if (!url) return;
    try {
        const payload = await requestJson(url);
        renderMemorySections(payload.sections || []);
    } catch (error) {
        showFeedback(error.message || "加载记忆失败", "error");
    }
}
function setMemoryCardEditing(card, editing) {
    const text = card.querySelector("[data-memory-text]");
    const editor = card.querySelector("[data-memory-editor]");
    const edit = card.querySelector("[data-memory-edit]");
    const save = card.querySelector("[data-memory-save]");
    const cancel = card.querySelector("[data-memory-cancel]");
    const del = card.querySelector("[data-memory-delete]");
    if (!text || !editor || !edit || !save || !cancel || !del) return;
    text.classList.toggle("hidden", editing);
    editor.classList.toggle("hidden", !editing);
    edit.classList.toggle("hidden", editing);
    save.classList.toggle("hidden", !editing);
    cancel.classList.toggle("hidden", !editing);
    del.disabled = editing;
    if (editing) editor.focus();
}

function bindMemoryBoard() {
    const board = document.getElementById("memoryBoard");
    if (!board) return;
    board.addEventListener("click", async (event) => {
        const card = event.target.closest(".memory-card");
        if (!card) return;
        if (event.target.closest("[data-memory-edit]")) {
            setMemoryCardEditing(card, true);
            return;
        }
        if (event.target.closest("[data-memory-cancel]")) {
            const editor = card.querySelector("[data-memory-editor]");
            const text = card.querySelector("[data-memory-text]");
            if (editor && text) editor.value = text.textContent || "";
            setMemoryCardEditing(card, false);
            return;
        }
        if (event.target.closest("[data-memory-save]")) {
            const editor = card.querySelector("[data-memory-editor]");
            try {
                await requestJson(CLIENT_CONFIG.urls.memoryUpdate, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": getCsrfToken()
                    },
                    body: JSON.stringify({
                        id: card.dataset.memoryId,
                        kind: card.dataset.memoryKind,
                        owner_user_id: card.dataset.memoryOwner,
                        content: editor?.value || ""
                    })
                });
                showFeedback("记忆已更新", "success");
                await loadMemoryItems();
            } catch (error) {
                showFeedback(error.message || "更新记忆失败", "error");
            }
            return;
        }
        if (event.target.closest("[data-memory-delete]")) {
            if (!window.confirm("确定要删除这条记忆吗？")) return;
            try {
                await requestJson(CLIENT_CONFIG.urls.memoryDelete, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": getCsrfToken()
                    },
                    body: JSON.stringify({
                        id: card.dataset.memoryId,
                        kind: card.dataset.memoryKind,
                        owner_user_id: card.dataset.memoryOwner
                    })
                });
                showFeedback("记忆已删除", "success");
                await loadMemoryItems();
            } catch (error) {
                showFeedback(error.message || "删除记忆失败", "error");
            }
        }
    });
}

async function loadRecall() {
    const url = CLIENT_CONFIG.urls.recall;
    if (!url) return;
    try {
        const payload = await requestJson(url);
        const list = document.getElementById("recallList");
        const empty = document.getElementById("recallEmptyState");
        if (!list || !empty) return;
        const items = Array.isArray(payload.items) ? payload.items : [];
        if (!items.length) {
            list.innerHTML = "";
            list.classList.add("hidden");
            empty.classList.remove("hidden");
            return;
        }
        empty.classList.add("hidden");
        list.classList.remove("hidden");
        list.innerHTML = items.map((item) => `<article class="recall-item"><h4>${escapeHtml(item.title || "回忆片段")}</h4><p>${escapeHtml(item.content || "")}</p></article>`).join("");
    } catch {
        const list = document.getElementById("recallList");
        const empty = document.getElementById("recallEmptyState");
        if (list) list.classList.add("hidden");
        if (empty) empty.classList.remove("hidden");
    }
}

document.addEventListener("DOMContentLoaded", () => {
    bindNavigation();
    bindTheme();
    bindSidebar();
    bindColors();
    bindModal();
    bindSaveActions();
    bindVisualEditors();
    bindDirtyTracking();
    bindTooltipLayer();
    bindOptionPicker({
        triggerId: "strategyTrigger",
        cardId: "strategyCard",
        inputId: "groupStrategyInput",
        labelId: "strategyTriggerLabel",
        optionSelector: ".strategy-option",
        valueKey: "value",
        labelKey: "label"
    });
    bindOptionPicker({
        triggerId: "memoryScopeTrigger",
        cardId: "memoryScopeCard",
        inputId: "memoryReadScopeInput",
        labelId: "memoryScopeTriggerLabel",
        optionSelector: ".strategy-option",
        valueKey: "scopeValue",
        labelKey: "scopeLabel"
    });
    bindAvatarUpload();
    bindAvatarCropInteractions();
    bindMemoryBoard();
    restoreState();
    initDirtyState();
    updateRuntime(RUNTIME_DATA);
    pollRuntime();
    window.setInterval(pollRuntime, Number(CLIENT_CONFIG.refreshIntervalMs || 5000));
});


