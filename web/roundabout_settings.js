/*
 * ComfyUI-Roundabout 自定义设置菜单（前端扩展）
 *
 * - 入口挂在顶栏原生菜单「Roundabout → 设置…」（Vue 版前端官方扩展点
 *   commands + menuCommands，参考 KJNodes 的注册方式）；
 * - 点击弹出模态面板，含四个标签页：
 *     1) 工作流管理：列举 / 上传 / 删除节点 workflows/ 下的 JSON；
 *     2) 模型配置：读取并编辑 models.yaml，保存后热加载；
 *     3) Auto 映射：设置 model=auto 虚拟模型实际指向的真实模型；
 *     4) 队列监控：ComfyUI 执行队列 + 网关异步任务实时状态，可弹窗查看工作流 JSON。
 * - API Key 取自 localStorage（仅在开启鉴权时需要）。
 *
 * 不依赖任何 import map：直接用全局 `app` 与同域 `fetch`。
 *
 * 诊断：脚本一旦被执行会把 window.__ROUNDABOUT_EXT__ 写好；扩展注册成功会置
 * extRegistered=true。若菜单项不出现，可在浏览器控制台输入
 *   window.__ROUNDABOUT_EXT__
 * 看脚本是否加载、是否注册。
 */
(function () {
  "use strict";

  var EXT = "ComfyUI-Roundabout.Settings";
  var LS_KEY = "roundabout_api_key";
  // 诊断标记（方便排查脚本是否被前端加载执行）
  window.__ROUNDABOUT_EXT__ = { loaded: Date.now(), extRegistered: false };

  /* ---------------------------------------------------------------- 样式 */
  var css = [
    ".rb-overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;",
    "align-items:center;justify-content:center;z-index:10000;",
    "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}",
    ".rb-modal{width:min(980px,94vw);height:min(760px,92vh);display:flex;flex-direction:column;",
    "background:var(--bg-color,#fff);color:var(--fg-color,#111);border:1px solid rgba(136,136,136,.4);",
    "border-radius:12px;overflow:hidden;box-shadow:0 16px 48px rgba(0,0,0,.45);}",
    ".rb-header{display:flex;align-items:center;justify-content:space-between;",
    "padding:12px 16px;border-bottom:1px solid rgba(136,136,136,.3);}",
    ".rb-title{font-weight:600;font-size:15px;}",
    ".rb-sub{font-size:12px;opacity:.7;margin-top:2px;}",
    ".rb-close{cursor:pointer;border:none;background:transparent;font-size:20px;color:inherit;line-height:1;}",
    ".rb-tabs{display:flex;gap:4px;padding:8px 16px 0;border-bottom:1px solid rgba(136,136,136,.3);}",
    ".rb-tab{cursor:pointer;padding:7px 14px;border:1px solid transparent;border-bottom:none;",
    "border-radius:8px 8px 0 0;color:inherit;background:rgba(136,136,136,.12);font-size:13px;}",
    ".rb-tab.active{background:var(--bg-color,#fff);border-color:rgba(136,136,136,.4);font-weight:600;}",
    ".rb-body{flex:1;overflow:auto;padding:16px;}",
    ".rb-footer{display:flex;gap:8px;align-items:center;padding:10px 16px;border-top:1px solid rgba(136,136,136,.3);}",
    ".rb-btn{cursor:pointer;padding:7px 14px;border-radius:7px;border:1px solid rgba(136,136,136,.4);",
    "background:rgba(136,136,136,.15);color:inherit;font-size:13px;}",
    ".rb-btn:hover{filter:brightness(1.08);}",
    ".rb-btn.primary{background:#3b82f6;border-color:#3b82f6;color:#fff;}",
    ".rb-btn.danger{background:#ef4444;border-color:#ef4444;color:#fff;}",
    ".rb-btn:disabled{opacity:.5;cursor:not-allowed;}",
    ".rb-table{width:100%;border-collapse:collapse;font-size:13px;}",
    ".rb-table th,.rb-table td{text-align:left;padding:8px 10px;border-bottom:1px solid rgba(136,136,136,.2);vertical-align:middle;}",
    ".rb-table th{font-weight:600;opacity:.8;}",
    ".rb-mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;}",
    ".rb-tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;margin:1px 2px;",
    "background:rgba(136,136,136,.18);}",
    ".rb-tag.bad{background:rgba(239,68,68,.18);color:#dc2626;}",
    ".rb-tag.ok{background:rgba(34,197,94,.18);color:#16a34a;}",
    ".rb-msg{font-size:13px;padding:9px 12px;border-radius:7px;margin-bottom:12px;}",
    ".rb-msg.ok{background:rgba(34,197,94,.15);color:#15803d;}",
    ".rb-msg.err{background:rgba(239,68,68,.15);color:#b91c1c;}",
    ".rb-textarea{width:100%;height:100%;min-height:420px;box-sizing:border-box;",
    "font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;padding:12px;border-radius:8px;",
    "border:1px solid rgba(136,136,136,.4);background:var(--bg-color,#fff);color:var(--fg-color,#111);",
    "resize:vertical;}",
    ".rb-row{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;}",
    ".rb-input{flex:1;min-width:160px;padding:7px 10px;border-radius:7px;",
    "border:1px solid rgba(136,136,136,.4);background:var(--bg-color,#fff);color:var(--fg-color,#111);font-size:13px;}",
    ".rb-hint{font-size:12px;opacity:.7;}",
    ".rb-empty{opacity:.6;font-size:13px;padding:20px;text-align:center;}",
    ".rb-file{display:none;}",
    ".rb-fallback-btn{position:fixed;top:10px;right:10px;z-index:99999;}",
    ".rb-stat{display:inline-flex;flex-direction:column;align-items:center;gap:2px;padding:8px 18px;",
    "border-radius:9px;border:1px solid rgba(136,136,136,.3);background:rgba(136,136,136,.08);}",
    ".rb-stat b{font-size:20px;font-weight:700;}",
    ".rb-stat span{font-size:11px;opacity:.75;}",
    ".rb-status{display:inline-block;padding:2px 9px;border-radius:9px;font-size:11px;font-weight:600;}",
    ".rb-status.run{background:rgba(59,130,246,.18);color:#2563eb;}",
    ".rb-status.pend{background:rgba(234,179,8,.18);color:#b45309;}",
    ".rb-status.done{background:rgba(34,197,94,.18);color:#16a34a;}",
    ".rb-status.fail{background:rgba(239,68,68,.18);color:#dc2626;}",
    ".rb-status.wait{background:rgba(136,136,136,.18);color:#555;}",
    ".rb-json-modal{width:min(860px,92vw);height:min(720px,90vh);display:flex;flex-direction:column;",
    "background:var(--bg-color,#fff);color:var(--fg-color,#111);border:1px solid rgba(136,136,136,.4);",
    "border-radius:12px;overflow:hidden;box-shadow:0 16px 48px rgba(0,0,0,.45);}",
    ".rb-json-body{flex:1;overflow:auto;padding:12px;}",
    "@media (prefers-color-scheme:dark){.rb-tab{background:rgba(255,255,255,.08);}",
    ".rb-btn{background:rgba(255,255,255,.1);}}",
  ].join("");

  function injectStyle() {
    if (document.getElementById("rb-style")) return;
    var style = document.createElement("style");
    style.id = "rb-style";
    style.textContent = css;
    document.head.appendChild(style);
  }

  /* ---------------------------------------------------------------- 工具 */
  function el(tag, props, children) {
    var e = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        var v = props[k];
        if (v == null) return;
        if (k === "class") e.className = v;
        else if (k === "text") e.textContent = v;
        else if (k === "html") e.innerHTML = v;
        else if (k.indexOf("on") === 0 && typeof v === "function") e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v);
      });
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(function (c) {
        if (c == null) return;
        e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    }
    return e;
  }

  function appRef() {
    // 官方扩展入口：window.comfyAPI.app.app（KJNodes 同款）。window.app 是 GraphView
    // 组件 setup 时才赋值，扩展脚本加载时往往还不存在，只能当兜底。
    try { if (window.comfyAPI && window.comfyAPI.app && window.comfyAPI.app.app) return window.comfyAPI.app.app; } catch (e) {}
    try { if (window.app) return window.app; } catch (e) {}
    try { if (typeof app !== "undefined" && app) return app; } catch (e) {}
    return null;
  }

  function apiKey() { try { return localStorage.getItem(LS_KEY) || ""; } catch (e) { return ""; } }
  function setApiKey(k) { try { localStorage.setItem(LS_KEY, k || ""); } catch (e) {} }

  function headers(extra) {
    var h = {};
    if (extra) Object.keys(extra).forEach(function (k) { h[k] = extra[k]; });
    var k = apiKey();
    if (k) h["Authorization"] = "Bearer " + k;
    return h;
  }

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = headers(opts.headers || {});
    var res = await fetch(path, opts);
    var data = null;
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      var msg = (data && data.error && data.error.message) || ("HTTP " + res.status);
      throw new Error(msg);
    }
    return data;
  }

  function fmtSize(n) {
    if (n == null) return "-";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }
  function fmtTime(ts) {
    if (!ts) return "-";
    try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return "-"; }
  }

  /* ---------------------------------------------------------------- 模态面板 */
  var modal = null;
  var currentTab = "workflows";
  var bodyEl = null;
  var msgEl = null;

  function showMsg(type, text) {
    if (!msgEl) return;
    msgEl.className = "rb-msg " + type;
    msgEl.textContent = text;
    msgEl.style.display = "block";
  }
  function clearMsg() {
    if (msgEl) { msgEl.style.display = "none"; msgEl.textContent = ""; }
  }

  function openModal() {
    if (modal) { modal.style.display = "flex"; return; }
    msgEl = el("div", { style: "display:none;" });
    bodyEl = el("div", { class: "rb-body" });

    var tabWf = el("div", { class: "rb-tab", text: "工作流管理", onclick: function () { switchTab("workflows"); } });
    var tabModels = el("div", { class: "rb-tab", text: "模型配置 (models.yaml)", onclick: function () { switchTab("models"); } });
    var tabQueue = el("div", { class: "rb-tab", text: "队列监控", onclick: function () { switchTab("queue"); } });

    var keyInput = el("input", { class: "rb-input", type: "password", placeholder: "API Key（仅开启鉴权时需要）", value: apiKey() });
    keyInput.addEventListener("input", function () { setApiKey(keyInput.value.trim()); });

    var footer = el("div", { class: "rb-footer" }, [
      el("span", { class: "rb-hint", text: "API Key:" }),
      keyInput,
      el("span", { class: "rb-hint", id: "rb-state", text: "" }),
    ]);

    var modalBox = el("div", { class: "rb-modal" }, [
      el("div", { class: "rb-header" }, [
        el("div", {}, [
          el("div", { class: "rb-title", text: "ComfyUI-Roundabout 设置" }),
          el("div", { class: "rb-sub", id: "rb-sub", text: "管理生成工作流与模型配置" }),
        ]),
        el("button", { class: "rb-close", text: "×", title: "关闭", onclick: closeModal }),
      ]),
      el("div", { class: "rb-tabs" }, [tabWf, tabModels, tabQueue]),
      msgEl,
      bodyEl,
      footer,
    ]);

    modal = el("div", { class: "rb-overlay", onclick: function (ev) { if (ev.target === modal) closeModal(); } }, [modalBox]);
    document.body.appendChild(modal);

    modal._tabWf = tabWf;
    modal._tabModels = tabModels;
    modal._tabQueue = tabQueue;
    loadState();
    switchTab(currentTab);
  }

  function closeModal() {
    stopQueueAutoRefresh();
    if (modal) modal.style.display = "none";
  }

  function switchTab(tab) {
    currentTab = tab;
    if (modal) {
      modal._tabWf.className = "rb-tab" + (tab === "workflows" ? " active" : "");
      modal._tabModels.className = "rb-tab" + (tab === "models" ? " active" : "");
      modal._tabQueue.className = "rb-tab" + (tab === "queue" ? " active" : "");
    }
    clearMsg();
    if (tab === "workflows") renderWorkflows();
    else if (tab === "models") renderModels();
    else if (tab === "queue") renderQueue();
  }

  async function loadState() {
    try {
      var s = await api("/roundabout/admin/state");
      var sub = document.getElementById("rb-sub");
      if (sub) sub.textContent = "配置: " + s.models_file;
      var st = document.getElementById("rb-state");
      if (st) st.textContent = "模型 " + (s.models ? s.models.length : 0) + " · 鉴权 " + (s.auth_enabled ? "开" : "关");
    } catch (e) {
      var sub2 = document.getElementById("rb-sub");
      if (sub2) sub2.textContent = "无法连接管理端点: " + e.message;
    }
  }

  /* ---------------------------------------------------------- 标签页：工作流 */
  async function renderWorkflows() {
    if (!bodyEl) return;
    bodyEl.innerHTML = "";
    var fileInput = el("input", { class: "rb-file", type: "file", accept: ".json,application/json" });
    var nameInput = el("input", { class: "rb-input", type: "text", placeholder: "文件名（可选，默认取上传名）" });
    var uploadBtn = el("button", { class: "rb-btn primary", text: "上传工作流" });

    // —— 上传即创建模型条目 ——
    var createChk = el("input", { type: "checkbox" });
    var modelNameInput = el("input", { class: "rb-input", type: "text", placeholder: "模型名（如 my_sdxl）" });
    var modelDescInput = el("input", { class: "rb-input", type: "text", placeholder: "描述（可选）" });
    var createRow = el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;" }, [
      el("label", { style: "display:flex;gap:6px;align-items:center;font-size:13px;" }, [createChk, "同时创建模型条目"]),
      modelNameInput, modelDescInput,
    ]);

    uploadBtn.addEventListener("click", async function () {
      if (!fileInput.files || !fileInput.files.length) { showMsg("err", "请先选择一个 JSON 文件"); return; }
      var fd = new FormData();
      fd.append("file", fileInput.files[0]);
      var nm = nameInput.value.trim();
      if (nm) fd.append("name", nm);
      if (createChk.checked) {
        fd.append("create_model", "1");
        fd.append("model_name", modelNameInput.value.trim());
        fd.append("model_desc", modelDescInput.value.trim());
      }
      uploadBtn.disabled = true;
      try {
        var r = await api("/roundabout/admin/workflows/upload", { method: "POST", body: fd });
        var msg = "已上传 " + r.name + (r.valid ? "" : "（但校验未通过: " + (r.error || "") + "）");
        if (r.model_created) msg += "；已自动创建模型「" + r.model_created + "」（可到「模型配置」微调绑定）";
        showMsg("ok", msg);
        renderWorkflows();
      } catch (e) {
        showMsg("err", "上传失败: " + e.message);
      } finally {
        uploadBtn.disabled = false;
      }
    });

    var pickBtn = el("button", { class: "rb-btn", text: "选择文件" });
    pickBtn.addEventListener("click", function () { fileInput.click(); });
    fileInput.addEventListener("change", function () {
      if (fileInput.files && fileInput.files.length) nameInput.placeholder = "将使用: " + fileInput.files[0].name;
    });

    bodyEl.appendChild(el("div", { class: "rb-row" }, [pickBtn, fileInput, nameInput, uploadBtn]));
    bodyEl.appendChild(createRow);
    bodyEl.appendChild(el("div", { class: "rb-hint", text: "仅接受 ComfyUI「Workflow → Export (API)」导出的 JSON；UI 工作流会被拒绝。勾选「同时创建模型条目」后，将自动分析工作流并生成 models.yaml 条目（无需手编 YAML）。" }));

    var tableWrap = el("div", { style: "margin-top:14px;" });
    bodyEl.appendChild(tableWrap);
    tableWrap.appendChild(el("div", { class: "rb-hint", text: "载入中…" }));

    try {
      var data = await api("/roundabout/admin/workflows");
      tableWrap.innerHTML = "";
      if (!data.workflows.length) {
        tableWrap.appendChild(el("div", { class: "rb-empty", text: "workflows/ 目录为空，上传一个开始。" }));
        return;
      }
      var table = el("table", { class: "rb-table" }, [
        el("thead", {}, el("tr", {}, [
          el("th", { text: "文件名" }), el("th", { text: "大小" }), el("th", { text: "修改时间" }),
          el("th", { text: "校验" }), el("th", { text: "被哪些模型引用" }), el("th", { text: "操作" }),
        ])),
        el("tbody", {}),
      ]);
      var tbody = table.querySelector("tbody");
      data.workflows.forEach(function (wf) {
        var validTag = wf.valid
          ? el("span", { class: "rb-tag ok", text: "有效 (" + wf.nodes + " 节点)" })
          : el("span", { class: "rb-tag bad", text: "无效" });
        var errTag = wf.error ? el("div", { class: "rb-hint", text: wf.error }) : null;
        var used = wf.used_by && wf.used_by.length
          ? wf.used_by.map(function (m) { return el("span", { class: "rb-tag", text: m }); })
          : [el("span", { class: "rb-hint", text: "（未被引用）" })];
        var delBtn = el("button", { class: "rb-btn danger", text: "删除" });
        delBtn.addEventListener("click", function () { deleteWorkflow(wf.name, wf.used_by || []); });
        tbody.appendChild(el("tr", {}, [
          el("td", { class: "rb-mono", text: wf.name }),
          el("td", { text: fmtSize(wf.size) }),
          el("td", { text: fmtTime(wf.mtime) }),
          el("td", {}, [validTag, errTag]),
          el("td", {}, used),
          el("td", {}, [delBtn]),
        ]));
      });
      tableWrap.appendChild(table);
    } catch (e) {
      tableWrap.innerHTML = "";
      tableWrap.appendChild(el("div", { class: "rb-msg err", text: "加载失败: " + e.message }));
    }
  }

  async function deleteWorkflow(name, usedBy) {
    if (usedBy && usedBy.length) {
      showMsg("err", "该工作流正被模型引用（" + usedBy.join(", ") + "），请先在 models.yaml 中移除引用再删除。");
      return;
    }
    if (!confirm("确定删除工作流 " + name + " ？此操作不可撤销。")) return;
    try {
      await api("/roundabout/admin/workflows/" + encodeURIComponent(name), { method: "DELETE" });
      showMsg("ok", "已删除 " + name);
      renderWorkflows();
    } catch (e) {
      showMsg("err", "删除失败: " + e.message);
    }
  }

  /* ---------------------------------------------------------- 标签页：模型配置（结构化） */
  var modelsData = null;        // 结构化响应缓存
  var selectedModel = null;     // 当前选中的模型名
  var modelsTextarea = null;    // 原始 YAML 高级编辑
  var modelsRawMode = false;

  function coerceScalar(s) {
    if (typeof s !== "string") return s;
    var t = s.trim();
    if (t === "true") return true;
    if (t === "false") return false;
    if (t !== "" && !isNaN(Number(t))) return Number(t);
    return s;
  }
  function toScalar(v) {
    if (typeof v === "boolean") return v ? "true" : "false";
    if (v == null) return "";
    return String(v);
  }

  async function renderModels() {
    if (!bodyEl) return;
    bodyEl.innerHTML = "";
    if (modelsRawMode) { renderModelsRaw(); return; }

    var loading = el("div", { class: "rb-empty", text: "载入中…" });
    bodyEl.appendChild(loading);
    try {
      modelsData = await api("/roundabout/admin/models/structured");
    } catch (e) {
      bodyEl.innerHTML = "";
      bodyEl.appendChild(el("div", { class: "rb-msg err", text: "读取模型配置失败: " + e.message }));
      return;
    }
    bodyEl.innerHTML = "";

    var rawBtn = el("button", { class: "rb-btn", text: "原始 YAML（高级）", onclick: function () { modelsRawMode = !modelsRawMode; renderModels(); } });
    var newBtn = el("button", { class: "rb-btn primary", text: "+ 新建模型" });
    newBtn.addEventListener("click", function () {
      var base = "new_model", n = base, i = 1;
      var names = (modelsData.models || []).map(function (m) { return m.name; });
      while (names.indexOf(n) >= 0) { n = base + (i++); }
      modelsData.models.push({
        name: n, workflow: (modelsData.available_workflows || [])[0] || "",
        description: "", mode: "image", capabilities: ["text-to-image"],
        output_node: "", aliases: [], timeout: null, defaults: {}, bindings: {},
      });
      selectedModel = n; renderModels();
    });
    var topbar = el("div", { class: "rb-row", style: "margin-bottom:10px;" }, [
      newBtn, rawBtn, el("span", { class: "rb-hint", text: "结构化编辑，无需手编 YAML" }),
    ]);
    bodyEl.appendChild(topbar);

    var left = el("div", { style: "width:230px;flex:0 0 230px;border-right:1px solid rgba(136,136,136,.25);padding-right:8px;overflow:auto;" });
    var right = el("div", { style: "flex:1;min-width:0;padding-left:12px;overflow:auto;" });
    bodyEl.appendChild(el("div", { style: "display:flex;gap:0;height:calc(100% - 46px);" }, [left, right]));

    (modelsData.models || []).forEach(function (m) {
      var active = m.name === selectedModel;
      var item = el("div", {
        text: m.name,
        style: "padding:7px 9px;border-radius:7px;cursor:pointer;margin-bottom:4px;font-size:13px;" +
          (active ? "background:rgba(59,130,246,.18);border:1px solid #3b82f6;font-weight:600;"
                  : "border:1px solid transparent;opacity:.9;"),
        onclick: function () { selectedModel = m.name; renderModels(); },
      });
      left.appendChild(item);
    });

    if (!selectedModel && (modelsData.models || []).length) selectedModel = modelsData.models[0].name;
    var model = (modelsData.models || []).filter(function (m) { return m.name === selectedModel; })[0];
    if (!model) {
      right.appendChild(el("div", { class: "rb-empty", text: "没有模型，点「+ 新建模型」创建一个。" }));
    } else {
      buildModelForm(right, model);
    }
  }

  function field(labelText, control) {
    return el("div", { style: "margin-bottom:12px;" }, [
      el("div", { class: "rb-hint", style: "margin-bottom:4px;", text: labelText }), control,
    ]);
  }

  function buildModelForm(right, model) {
    // 名称（改名即改 key）
    var nameInput = el("input", { class: "rb-input", type: "text", value: model.name });
    nameInput.addEventListener("change", function () {
      var nv = nameInput.value.trim();
      if (!nv) { nameInput.value = model.name; return; }
      if (nv !== model.name && (modelsData.models || []).some(function (m) { return m.name === nv; })) {
        showMsg("err", "模型名 " + nv + " 已存在"); nameInput.value = model.name; return;
      }
      model.name = nv;
    });

    var descInput = el("input", { class: "rb-input", type: "text", value: model.description || "" });
    descInput.addEventListener("change", function () { model.description = descInput.value.trim(); });

    // workflow 下拉
    var wfSel = el("select", { class: "rb-input" });
    (modelsData.available_workflows || []).forEach(function (w) {
      var o = el("option", { value: w, text: w });
      if (w === model.workflow) o.selected = true;
      wfSel.appendChild(o);
    });
    if (!model.workflow) wfSel.appendChild(el("option", { value: "", text: "（未选择）", selected: true }));
    wfSel.addEventListener("change", function () { model.workflow = wfSel.value; });

    // mode
    var modeSel = el("select", { class: "rb-input" });
    [["image", "image（图片）"], ["video", "video（视频）"]].forEach(function (p) {
      var o = el("option", { value: p[0], text: p[1] });
      if (p[0] === model.mode) o.selected = true;
      modeSel.appendChild(o);
    });
    modeSel.addEventListener("change", function () { model.mode = modeSel.value; });

    // capabilities
    var capT = el("input", { type: "checkbox" });
    capT.checked = (model.capabilities || []).indexOf("text-to-image") >= 0;
    capT.addEventListener("change", function () { setCap(model, "text-to-image", capT.checked); });
    var capI = el("input", { type: "checkbox" });
    capI.checked = (model.capabilities || []).indexOf("image-to-image") >= 0;
    capI.addEventListener("change", function () { setCap(model, "image-to-image", capI.checked); });
    var capRow = el("div", { style: "display:flex;gap:16px;align-items:center;" }, [
      el("label", { style: "display:flex;gap:6px;align-items:center;font-size:13px;" }, [capT, "text-to-image"]),
      el("label", { style: "display:flex;gap:6px;align-items:center;font-size:13px;" }, [capI, "image-to-image"]),
    ]);

    var outInput = el("input", { class: "rb-input", type: "text", value: model.output_node || "", placeholder: "如 9 或 57:27" });
    outInput.addEventListener("change", function () { model.output_node = outInput.value.trim() || null; });

    var aliasInput = el("input", { class: "rb-input", type: "text", value: (model.aliases || []).join(", "), placeholder: "逗号分隔" });
    aliasInput.addEventListener("change", function () {
      model.aliases = aliasInput.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    });

    var timeoutInput = el("input", { class: "rb-input", type: "number", value: model.timeout == null ? "" : String(model.timeout), placeholder: "留空=默认" });
    timeoutInput.addEventListener("change", function () {
      var v = timeoutInput.value.trim(); model.timeout = v === "" ? null : (Number(v) || null);
    });

    right.appendChild(field("模型名称", nameInput));
    right.appendChild(field("描述", descInput));
    right.appendChild(field("工作流文件", wfSel));
    right.appendChild(field("模式", modeSel));
    right.appendChild(field("能力", capRow));
    right.appendChild(field("输出节点 ID", outInput));
    right.appendChild(field("别名（逗号分隔）", aliasInput));
    right.appendChild(field("超时（秒）", timeoutInput));

    // ---- 默认参数（动态键值）----
    var dlist = Object.keys(model.defaults || {}).map(function (k) {
      return { k: k, v: toScalar(model.defaults[k]) };
    });
    var dWrap = el("div", {});
    function renderDefaults() {
      dWrap.innerHTML = "";
      dlist.forEach(function (row, idx) {
        var kIn = el("input", { class: "rb-input", type: "text", value: row.k, style: "flex:1;min-width:90px;" });
        var vIn = el("input", { class: "rb-input", type: "text", value: row.v, style: "flex:1;min-width:90px;" });
        kIn.addEventListener("change", function () { dlist[idx].k = kIn.value.trim(); });
        vIn.addEventListener("change", function () { dlist[idx].v = vIn.value; });
        var rm = el("button", { class: "rb-btn danger", text: "×", title: "删除", onclick: function () { dlist.splice(idx, 1); renderDefaults(); } });
        dWrap.appendChild(el("div", { style: "display:flex;gap:6px;margin-bottom:6px;" }, [kIn, vIn, rm]));
      });
      model.defaults = dlist;
    }
    renderDefaults();
    var addD = el("button", { class: "rb-btn", text: "+ 添加默认参数", onclick: function () { dlist.push({ k: "", v: "" }); renderDefaults(); } });
    right.appendChild(field("默认参数（覆盖共享默认值）", el("div", {}, [dWrap, addD])));

    // ---- 绑定（语义参数 -> 节点路径）----
    var blist = Object.keys(model.bindings || {}).map(function (k) {
      var v = model.bindings[k];
      return { k: k, v: Array.isArray(v) ? v.join(", ") : String(v) };
    });
    var bWrap = el("div", {});
    var dl = el("datalist", { id: "rb-known-params" });
    (modelsData.known_params || []).forEach(function (p) { dl.appendChild(el("option", { value: p })); });
    function renderBindings() {
      bWrap.innerHTML = "";
      blist.forEach(function (row, idx) {
        var kIn = el("input", { class: "rb-input", type: "text", value: row.k, list: "rb-known-params", style: "flex:1;min-width:120px;", placeholder: "语义参数" });
        var vIn = el("input", { class: "rb-input", type: "text", value: row.v, style: "flex:2;min-width:160px;", placeholder: "如 6.inputs.text（多路径逗号分隔）" });
        kIn.addEventListener("change", function () { blist[idx].k = kIn.value.trim(); });
        vIn.addEventListener("change", function () { blist[idx].v = vIn.value.trim(); });
        var rm = el("button", { class: "rb-btn danger", text: "×", title: "删除", onclick: function () { blist.splice(idx, 1); renderBindings(); } });
        bWrap.appendChild(el("div", { class: "rb-mono", style: "display:flex;gap:6px;margin-bottom:6px;" }, [kIn, vIn, rm]));
      });
      model.bindings = blist;
    }
    renderBindings();
    var addB = el("button", { class: "rb-btn", text: "+ 添加绑定", onclick: function () { blist.push({ k: "", v: "" }); renderBindings(); } });
    right.appendChild(field("参数绑定（语义参数 -> 工作流节点路径）", el("div", {}, [dl, bWrap, addB])));

    // ---- 底部操作 ----
    var saveBtn = el("button", { class: "rb-btn primary", text: "保存全部模型配置" });
    var delBtn = el("button", { class: "rb-btn danger", text: "删除此模型" });
    var reloadBtn = el("button", { class: "rb-btn", text: "重新加载" });
    saveBtn.addEventListener("click", saveStructured);
    delBtn.addEventListener("click", function () { deleteSelectedModel(model.name); });
    reloadBtn.addEventListener("click", reloadRegistry);
    right.appendChild(el("div", { class: "rb-row", style: "margin-top:8px;padding-top:10px;border-top:1px solid rgba(136,136,136,.25);" },
      [saveBtn, delBtn, reloadBtn, el("span", { class: "rb-hint", text: "保存时校验 workflow 与绑定路径；失败不覆盖" })]));
  }

  function setCap(model, cap, on) {
    var caps = model.capabilities || [];
    var i = caps.indexOf(cap);
    if (on && i < 0) caps.push(cap);
    if (!on && i >= 0) caps.splice(i, 1);
    model.capabilities = caps;
  }

  function buildStructuredPayload() {
    var models = (modelsData.models || []).map(function (m) {
      var o = {
        name: m.name, workflow: m.workflow, description: m.description || "",
        mode: m.mode || "image", capabilities: m.capabilities || ["text-to-image"],
        output_node: m.output_node || null, aliases: m.aliases || [],
        timeout: m.timeout == null ? null : m.timeout, defaults: {}, bindings: {},
      };
      // defaults 可能是编辑用的数组
      var dsrc = Array.isArray(m.defaults) ? m.defaults : Object.keys(m.defaults || {}).map(function (k) { return { k: k, v: toScalar(m.defaults[k]) }; });
      dsrc.forEach(function (row) {
        if (row.k && row.k.trim()) o.defaults[row.k.trim()] = coerceScalar(row.v);
      });
      var bsrc = Array.isArray(m.bindings) ? m.bindings : Object.keys(m.bindings || {}).map(function (k) { var v = m.bindings[k]; return { k: k, v: Array.isArray(v) ? v.join(", ") : String(v) }; });
      bsrc.forEach(function (row) {
        if (!row.k || !row.k.trim()) return;
        var parts = row.v.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        o.bindings[row.k.trim()] = parts.length > 1 ? parts : (parts[0] || "");
      });
      return o;
    });
    return {
      default_model: modelsData.default_model || null,
      shared: modelsData.shared || {},
      models: models,
    };
  }

  async function saveStructured() {
    try {
      showMsg("ok", "校验中…");
      var payload = buildStructuredPayload();
      var r = await api("/roundabout/admin/models/structured", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      showMsg("ok", "已保存并热加载。当前模型: " + (r.models || []).join(", "));
      loadState();
      renderModels();
    } catch (e) {
      showMsg("err", "保存失败: " + e.message);
    }
  }

  async function deleteSelectedModel(name) {
    if (!confirm("确定删除模型 " + name + " ？此操作不可撤销。")) return;
    try {
      var r = await api("/roundabout/admin/models/" + encodeURIComponent(name), { method: "DELETE" });
      showMsg("ok", "已删除 " + name);
      selectedModel = null;
      renderModels();
      loadState();
    } catch (e) {
      showMsg("err", "删除失败: " + e.message);
    }
  }

  /* ---------------------------------------------------------- 标签页：队列监控 */
  var queueTimer = null;
  var queueBodies = { running: null, pending: null, tasks: null, stats: null };

  function fmtDur(s) {
    if (s == null || isNaN(s)) return "-";
    if (s < 60) return s.toFixed(1) + "s";
    var m = Math.floor(s / 60), r = Math.round(s % 60);
    return m + "m" + (r < 10 ? "0" : "") + r + "s";
  }
  function shortId(id) {
    if (!id) return "-";
    return id.length > 12 ? id.slice(0, 8) + "…" + id.slice(-4) : id;
  }
  function statusBadge(status) {
    var map = {
      queued: ["wait", "待处理"], processing: ["run", "执行中"],
      succeeded: ["done", "已完成"], failed: ["fail", "失败"],
      running: ["run", "执行中"], pending: ["pend", "排队中"],
    };
    var m = map[status] || ["wait", status || "?"];
    return el("span", { class: "rb-status " + m[0], text: m[1] });
  }

  async function renderQueue() {
    if (!bodyEl) return;
    bodyEl.innerHTML = "";
    queueBodies = { running: null, pending: null, tasks: null, stats: null };

    // 顶部：统计卡 + 刷新/自动刷新
    var statsRow = el("div", { class: "rb-row", style: "margin-bottom:12px;" }, [
      el("div", { class: "rb-stat", id: "rbq-run", html: "<b>0</b><span>执行中 (ComfyUI)</span>" }),
      el("div", { class: "rb-stat", id: "rbq-pend", html: "<b>0</b><span>排队中 (ComfyUI)</span>" }),
      el("div", { class: "rb-stat", id: "rbq-task", html: "<b>0</b><span>网关异步任务</span>" }),
    ]);
    var refreshBtn = el("button", { class: "rb-btn", text: "刷新" });
    refreshBtn.addEventListener("click", function () { refreshQueue(); });
    var autoChk = el("input", { type: "checkbox", checked: true });
    autoChk.addEventListener("change", function () {
      if (autoChk.checked) startQueueAutoRefresh(); else stopQueueAutoRefresh();
    });
    var autoLbl = el("label", { style: "display:flex;gap:6px;align-items:center;font-size:13px;" }, [autoChk, "自动刷新 (3s)"]);
    var hint = el("span", { class: "rb-hint", text: "网关层队列 = ComfyUI 执行队列 + 本网关异步任务表" });
    bodyEl.appendChild(statsRow);
    bodyEl.appendChild(el("div", { class: "rb-row" }, [refreshBtn, autoLbl, hint]));

    // 三个区块容器
    var runWrap = el("div", {}); var pendWrap = el("div", {}); var taskWrap = el("div", {});
    bodyEl.appendChild(runWrap); bodyEl.appendChild(pendWrap); bodyEl.appendChild(taskWrap);
    queueBodies.running = runWrap; queueBodies.pending = pendWrap; queueBodies.tasks = taskWrap;

    await refreshQueue();
    startQueueAutoRefresh();
  }

  function startQueueAutoRefresh() {
    stopQueueAutoRefresh();
    queueTimer = setInterval(function () {
      if (modal && modal.style.display !== "none" && currentTab === "queue") refreshQueue();
    }, 3000);
  }
  function stopQueueAutoRefresh() {
    if (queueTimer) { clearInterval(queueTimer); queueTimer = null; }
  }

  function queueTable(title, cols, rows, emptyText) {
    var wrap = el("div", { style: "margin-top:14px;" });
    wrap.appendChild(el("div", { class: "rb-hint", style: "margin-bottom:6px;font-weight:600;", text: title }));
    if (!rows || !rows.length) {
      wrap.appendChild(el("div", { class: "rb-empty", text: emptyText }));
      return wrap;
    }
    var table = el("table", { class: "rb-table" }, [
      el("thead", {}, el("tr", {}, cols.map(function (c) { return el("th", { text: c }); }))),
      el("tbody", {}),
    ]);
    var tbody = table.querySelector("tbody");
    rows.forEach(function (r) { tbody.appendChild(r); });
    wrap.appendChild(table);
    return wrap;
  }

  function workflowBtn(promptId, status) {
    var b = el("button", { class: "rb-btn", text: "工作流 JSON" });
    b.addEventListener("click", function () { showWorkflowModal(promptId, status); });
    return b;
  }

  function buildComfyRow(item) {
    var timeCell = (item.created ? fmtTime(item.created) : "-") +
      (item.elapsed != null ? "（" + fmtDur(item.elapsed) + "）" : "");
    return el("tr", {}, [
      el("td", { class: "rb-mono", text: item.number }),
      el("td", { class: "rb-mono", title: item.prompt_id, text: shortId(item.prompt_id) }),
      el("td", { class: "rb-mono", title: item.seed != null ? String(item.seed) : "", text: item.seed != null ? shortId(String(item.seed)) : "-" }),
      el("td", { text: timeCell }),
      el("td", { text: item.node_count != null ? item.node_count : "-" }),
      el("td", {}, [workflowBtn(item.prompt_id, item._status)]),
    ]);
  }

  function buildTaskRow(t) {
    var tcell = (t.created ? fmtTime(t.created) : "-") + "（" + fmtDur(t.elapsed) + "）";
    // 被外部取消（JobCancelled, code=409）显示「已取消」徽标
    var badge = (t.status === "failed" && t.code === 409)
      ? el("span", { class: "rb-status wait", text: "已取消" })
      : statusBadge(t.status);
    // 工作流查询：优先用 task id（快照），history 侧用 prompt_id 也能兜底
    var wfId = t.id;
    var wfBtn;
    if (t.has_workflow || t.prompt_id) {
      wfBtn = workflowBtn(wfId, t.status);
    } else if (t.status === "processing" || t.status === "queued") {
      // 还没提交到 ComfyUI（在网关并发信号量排队等名额），无 prompt_id 是正常的
      wfBtn = el("button", { class: "rb-btn", disabled: "disabled", text: "工作流 JSON", title: "尚未提交到 ComfyUI（正在等待并发名额），提交后即可查看" });
    } else {
      wfBtn = el("button", { class: "rb-btn", disabled: "disabled", text: "工作流 JSON", title: "提交前失败，无工作流快照" });
    }
    return el("tr", {}, [
      el("td", { class: "rb-mono", title: t.id, text: shortId(t.id) }),
      el("td", {}, [badge]),
      el("td", { class: "rb-mono", text: t.model || "-" }),
      el("td", { class: "rb-mono", title: t.prompt_id || "", text: t.prompt_id ? shortId(t.prompt_id) : "-" }),
      el("td", { class: "rb-mono", title: t.seed != null ? String(t.seed) : "", text: t.seed != null ? shortId(String(t.seed)) : "-" }),
      el("td", { class: "rb-mono", title: t.request_id || "", text: t.request_id ? shortId(t.request_id) : "-" }),
      el("td", { text: tcell }),
      el("td", {}, [wfBtn]),
    ]);
  }

  async function refreshQueue() {
    try {
      var d = await api("/roundabout/admin/queue");
    } catch (e) {
      if (queueBodies.stats) queueBodies.stats.textContent = "读取失败: " + e.message;
      return;
    }
    var setNum = function (id, n) { var x = document.getElementById(id); if (x) x.querySelector("b").textContent = n; };
    setNum("rbq-run", d.running_count || 0);
    setNum("rbq-pend", d.pending_count || 0);
    setNum("rbq-task", (d.tasks || []).length);

    var r = (d.running || []).map(function (it) { it._status = "running"; return buildComfyRow(it); });
    var p = (d.pending || []).map(function (it) { it._status = "pending"; return buildComfyRow(it); });
    var t = (d.tasks || []).map(buildTaskRow);

    queueBodies.running.innerHTML = "";
    queueBodies.running.appendChild(queueTable("执行中（ComfyUI）", ["#", "prompt_id", "seed", "入队时间（已耗时）", "节点数", "操作"], r, "当前没有执行中的任务"));
    queueBodies.pending.innerHTML = "";
    queueBodies.pending.appendChild(queueTable("排队中（ComfyUI）", ["#", "prompt_id", "seed", "入队时间（已耗时）", "节点数", "操作"], p, "队列为空，没有等待的任务"));
    queueBodies.tasks.innerHTML = "";
    queueBodies.tasks.appendChild(queueTable("网关异步任务", ["任务 ID", "状态", "模型", "prompt_id", "seed", "请求 ID", "创建时间（已耗时）", "操作"], t, "暂无异步任务"));
  }

  /* ---------------------------------------------------------- 工作流 JSON 弹窗 */
  async function showWorkflowModal(promptId, status) {
    var ov = el("div", { class: "rb-overlay", style: "z-index:10002;", onclick: function (ev) { if (ev.target === ov) closeJson(); } });
    var textarea = el("textarea", { class: "rb-textarea", readonly: "readonly", style: "min-height:0;height:100%;" });
    var hint = el("span", { class: "rb-hint", text: "加载中…" });

    var copyBtn = el("button", { class: "rb-btn", text: "复制" });
    copyBtn.addEventListener("click", function () {
      navigator.clipboard.writeText(textarea.value).then(function () { copyBtn.textContent = "已复制"; }, function () { copyBtn.textContent = "复制失败"; });
    });
    var dlBtn = el("button", { class: "rb-btn", text: "下载 .json" });
    dlBtn.addEventListener("click", function () {
      var blob = new Blob([textarea.value], { type: "application/json" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "workflow-" + promptId.slice(0, 8) + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
    });
    var closeBtn = el("button", { class: "rb-btn primary", text: "关闭" });
    closeBtn.addEventListener("click", closeJson);

    function closeJson() {
      if (ov.parentNode) ov.parentNode.removeChild(ov);
    }

    ov.appendChild(el("div", { class: "rb-json-modal" }, [
      el("div", { class: "rb-header" }, [
        el("div", {}, [
          el("div", { class: "rb-title", text: "工作流 JSON" }),
          el("div", { class: "rb-sub", text: "prompt_id: " + promptId + " · " + status }),
        ]),
        el("button", { class: "rb-close", text: "×", onclick: closeJson }),
      ]),
      el("div", { class: "rb-json-body" }, [textarea]),
      el("div", { class: "rb-footer" }, [copyBtn, dlBtn, closeBtn, hint]),
    ]));
    document.body.appendChild(ov);

    try {
      var d = await api("/roundabout/admin/queue/workflow/" + encodeURIComponent(promptId));
      textarea.value = JSON.stringify(d.workflow, null, 2);
      var src = d.source === "queue" ? "队列" : (d.source === "history" ? "执行历史" : (d.source === "task" ? "任务快照" : d.source));
      hint.textContent = (d.workflow ? Object.keys(d.workflow).length + " 个节点" : "（无工作流数据）") + " · 来源: " + src;
    } catch (e) {
      textarea.value = "";
      hint.textContent = "获取失败: " + e.message;
    }
  }

  /* ---------------------------------------------------------- 原始 YAML（高级兜底） */
  async function renderModelsRaw() {
    if (!bodyEl) return;
    bodyEl.innerHTML = "";
    modelsTextarea = el("textarea", { class: "rb-textarea", placeholder: "载入中…" });
    bodyEl.appendChild(modelsTextarea);
    var saveBtn = el("button", { class: "rb-btn primary", text: "保存并热加载" });
    var backBtn = el("button", { class: "rb-btn", text: "返回结构化" });
    var hint = el("div", { class: "rb-hint", text: "高级：直接编辑原始 YAML。保存前会校验；失败不覆盖。" });
    saveBtn.addEventListener("click", saveModelsRaw);
    backBtn.addEventListener("click", function () { modelsRawMode = false; renderModels(); });
    bodyEl.appendChild(el("div", { class: "rb-row", style: "margin-top:12px;" }, [saveBtn, backBtn, hint]));
    try {
      var data = await api("/roundabout/admin/models");
      modelsTextarea.value = data.content || "";
    } catch (e) {
      modelsTextarea.value = "";
      showMsg("err", "读取 models.yaml 失败: " + e.message);
    }
  }

  async function saveModelsRaw() {
    if (!modelsTextarea) return;
    try {
      showMsg("ok", "校验中…");
      var r = await api("/roundabout/admin/models", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: modelsTextarea.value }),
      });
      showMsg("ok", "已保存并热加载。当前模型: " + (r.models || []).join(", "));
      loadState();
    } catch (e) {
      showMsg("err", "保存失败: " + e.message);
    }
  }

  async function reloadRegistry() {
    try {
      var r = await api("/admin/reload", { method: "POST" });
      showMsg("ok", "已重新加载。模型: " + (r.models || []).join(", "));
      loadState();
    } catch (e) {
      showMsg("err", "重载失败: " + e.message);
    }
  }

  /* ---------------------------------------------------------------- 注册
   * 入口为顶栏原生菜单（Vue 版前端官方扩展点）：
   *   commands   —— 注册命令对象（id + menubarLabel + function）
   *   menuCommands —— 把命令挂进菜单路径，path[0] 自动成为顶级菜单
   * 不再往 body 挂固定悬浮按钮（避免与新版顶栏/主题冲突）。
   *
   * 时序：扩展脚本可能先于前端 app 就绪执行，register() 若拿不到 app 就
   * 延迟重试（最多 ~6s），并在 app 可用时同时走 registerExtension + 手动
   * loadExtensionMenuCommands，保证菜单注册不依赖前端内部时序。
   */
  var _registered = false;

  function registerExt() {
    var app = appRef();
    if (!app || typeof app.registerExtension !== "function") return false;

    var ext = {
      name: EXT,

      // 菜单命令：四个标签页直接作为「Roundabout」菜单项，点击直达对应 tab
      commands: [
        {
          id: "Roundabout.Tabs.workflows",
          label: "工作流管理",
          menubarLabel: "工作流管理",
          function: function () { openTab("workflows"); },
        },
        {
          id: "Roundabout.Tabs.models",
          label: "模型配置",
          menubarLabel: "模型配置",
          function: function () { openTab("models"); },
        },
        {
          id: "Roundabout.Tabs.queue",
          label: "队列监控",
          menubarLabel: "队列监控",
          function: function () { openTab("queue"); },
        },
      ],

      // 挂到顶级菜单「Roundabout」下（path[0] 即顶级菜单名）
      menuCommands: [
        {
          path: ["Roundabout"],
          commands: [
            "Roundabout.Tabs.workflows",
            "Roundabout.Tabs.models",
            "Roundabout.Tabs.queue",
          ],
        },
      ],

      init: function () { injectStyle(); },
      setup: function () {},
    };

    app.registerExtension(ext);
    if (window.__ROUNDABOUT_EXT__) window.__ROUNDABOUT_EXT__.extRegistered = true;
    return true;
  }

  // 打开面板并切换到指定标签页（供菜单命令调用）
  function openTab(tab) {
    openModal();
    switchTab(tab);
  }

  function register() {
    injectStyle();
    if (registerExt()) { _registered = true; return; }
    // app 未就绪：延迟重试（前端 bundle 加载完成后 window.comfyAPI.app.app 才存在）
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      if (registerExt()) { _registered = true; clearInterval(timer); }
      else if (tries > 40) { clearInterval(timer); }  // ~6s 后放弃，避免空转
    }, 150);
  }

  register();
})();
