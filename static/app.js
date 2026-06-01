const state = {
  user: null,
  projects: [],
  batches: [],
  selectedProjectId: "all",
  view: "table",
};

const fileLabels = {
  compound_info: "化合物文件",
  bio_raw_data: "生物原始数据",
  data_summary: "数据整理文档",
};

const filePermissions = {
  compound_info: ["manager", "chem"],
  bio_raw_data: ["manager", "bio"],
  data_summary: ["manager", "bio"],
};

const fileAccepts = {
  compound_info: ".xlsx,.xls,.xlsm,.csv,.pdf,.doc,.docx",
  bio_raw_data: ".xlsx,.xls,.xlsm,.csv",
  data_summary: ".xlsx,.xls,.xlsm,.csv,.pdf,.doc,.docx",
};

const multiFileTypes = new Set(["compound_info", "data_summary"]);

const statusMeta = [
  { key: "done", label: "已完成", cls: "done" },
  { key: "bio", label: "测试中", cls: "bio" },
  { key: "wait", label: "待测试", cls: "wait" },
  { key: "synth", label: "合成中", cls: "synth" },
  { key: "todo", label: "待合成", cls: "todo" },
];

const $ = (selector) => document.querySelector(selector);

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 2600);
}

async function api(path, options = {}) {
  const headers = options.headers || {};
  const init = { ...options, headers };
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  if (res.status === 204) return null;
  const contentType = res.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    throw new Error(payload.error || payload || "请求失败");
  }
  return payload;
}

function roleLabel(role) {
  return { manager: "总负责人", chem: "化学部门", bio: "生物部门" }[role] || role;
}

function canEdit(field) {
  const role = state.user?.role;
  if (role === "manager") return true;
  if (["batch_no", "name"].includes(field)) return role === "chem";
  if (["synthesis_submitted_date", "synthesis_completed_date"].includes(field)) return role === "chem";
  if (["bio_test_start_date", "bio_test_completed_date"].includes(field)) return role === "bio";
  return false;
}

function canUpload(fileType) {
  return filePermissions[fileType]?.includes(state.user?.role);
}

function getStatus(batch) {
  if (batch.bio_test_completed_date) return statusMeta[0];
  if (batch.bio_test_start_date) return statusMeta[1];
  if (batch.synthesis_completed_date) return statusMeta[2];
  if (batch.synthesis_submitted_date) return statusMeta[3];
  return statusMeta[4];
}

function formatSize(bytes) {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function shortTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-CN", { hour12: false });
}

async function boot() {
  try {
    const me = await api("/api/me");
    state.user = me.user;
    $("#loginView").classList.add("hidden");
    $("#appView").classList.remove("hidden");
    renderUser();
    await loadData();
  } catch {
    $("#loginView").classList.remove("hidden");
    $("#appView").classList.add("hidden");
  }
}

function renderUser() {
  const displayName = state.user.username === "leader" ? "Leader" : state.user.display_name;
  $("#userBadge").innerHTML = `<strong>${displayName}</strong>`;
  document.querySelectorAll(".manager-only").forEach((el) => {
    el.classList.toggle("hidden", state.user.role !== "manager");
  });
  document.querySelectorAll(".batch-create-only").forEach((el) => {
    el.classList.toggle("hidden", !["manager", "chem"].includes(state.user.role));
  });
}

async function loadData() {
  const [projectsPayload, batchesPayload] = await Promise.all([
    api("/api/projects"),
    api(`/api/batches?project_id=${state.selectedProjectId}`),
  ]);
  state.projects = projectsPayload.projects;
  state.batches = batchesPayload.batches;
  renderAll();
}

function renderAll() {
  renderProjects();
  renderHeader();
  renderStats();
  renderTable();
  renderGantt();
}

function renderProjects() {
  $("#allProjectsBtn").classList.toggle("active", state.selectedProjectId === "all");
  const list = $("#projectList");
  list.innerHTML = "";
  state.projects.forEach((project) => {
    const row = document.createElement("div");
    row.className = "project-row";
    const btn = document.createElement("button");
    btn.className = "project-item";
    btn.textContent = project.name;
    btn.type = "button";
    btn.classList.toggle("active", String(project.id) === String(state.selectedProjectId));
    btn.addEventListener("click", async () => {
      state.selectedProjectId = String(project.id);
      await loadData();
    });
    row.appendChild(btn);
    list.appendChild(row);
  });
}

function renderHeader() {
  const project = state.projects.find((item) => String(item.id) === String(state.selectedProjectId));
  $("#pageTitle").textContent = project ? project.name : "全部项目";
  $("#pageSubtitle").textContent = project ? "当前项目批次状态与文件版本" : "查看全部批次状态与文件版本";
  $("#deleteProjectBtn").classList.toggle("hidden", state.user.role !== "manager" || !project);
  $("#tableView").classList.toggle("hidden", state.view !== "table");
  $("#ganttView").classList.toggle("hidden", state.view !== "gantt");
  $("#tableTab").classList.toggle("active", state.view === "table");
  $("#ganttTab").classList.toggle("active", state.view === "gantt");
}

function renderStats() {
  const total = state.batches.length;
  const counts = { synth: 0, bio: 0, done: 0 };
  state.batches.forEach((batch) => {
    const key = getStatus(batch).key;
    if (key in counts) counts[key] += 1;
  });
  $("#statTotal").textContent = total;
  $("#statSynthesis").textContent = counts.synth;
  $("#statBio").textContent = counts.bio;
  $("#statDone").textContent = counts.done;
}

function renderTable() {
  const body = $("#batchTable");
  body.innerHTML = "";
  $("#emptyState").classList.toggle("hidden", state.batches.length > 0);
  state.batches.forEach((batch) => {
    const tr = document.createElement("tr");
    tr.append(
      textCell(batch.project_name),
      inputCell(batch, "batch_no", "text", canEdit("batch_no"), "batch-no-input"),
      inputCell(batch, "name", "text", canEdit("name"), "wide-input"),
      statusCell(batch),
      inputCell(batch, "synthesis_submitted_date", "date", canEdit("synthesis_submitted_date")),
      inputCell(batch, "synthesis_completed_date", "date", canEdit("synthesis_completed_date")),
      inputCell(batch, "bio_test_start_date", "date", canEdit("bio_test_start_date")),
      inputCell(batch, "bio_test_completed_date", "date", canEdit("bio_test_completed_date")),
      fileCell(batch, "compound_info"),
      fileCell(batch, "bio_raw_data"),
      fileCell(batch, "data_summary"),
    );
    if (state.user.role === "manager") {
      tr.append(actionCell(batch));
    }
    body.appendChild(tr);
  });
}

function textCell(text) {
  const td = document.createElement("td");
  td.textContent = text || "";
  return td;
}

function statusCell(batch) {
  const td = document.createElement("td");
  const meta = getStatus(batch);
  td.innerHTML = `<span class="status ${meta.cls}">${meta.label}</span>`;
  return td;
}

function inputCell(batch, field, type, editable, className = "") {
  const td = document.createElement("td");
  const input = document.createElement("input");
  input.type = type;
  input.value = batch[field] || "";
  input.disabled = !editable;
  input.className = className;
  input.addEventListener("change", async () => {
    try {
      const payload = await api(`/api/batches/${batch.id}`, {
        method: "PATCH",
        body: { [field]: input.value },
      });
      replaceBatch(payload.batch);
      renderAll();
      showToast("已保存");
    } catch (err) {
      input.value = batch[field] || "";
      showToast(err.message);
    }
  });
  td.appendChild(input);
  return td;
}

function fileCell(batch, fileType) {
  const td = document.createElement("td");
  td.className = "file-cell";
  const bucket = batch.files[fileType] || { latest: null, versions: [] };
  const latest = bucket.latest;
  const name = document.createElement("div");
  name.className = "file-name";
  name.title = latest?.original_name || "";
  if (bucket.versions.length > 1) {
    name.textContent = `共 ${bucket.versions.length} 个文件`;
    name.title = bucket.versions.map((file) => file.original_name).join("\n");
  } else {
    name.textContent = latest ? latest.original_name : "暂无文件";
  }
  const actions = document.createElement("div");
  actions.className = "file-actions";

  if (latest) {
    const download = miniButton(bucket.versions.length > 1 ? "下载最新" : "下载");
    download.addEventListener("click", () => {
      window.location.href = `/api/files/${latest.id}/download`;
    });
    actions.appendChild(download);
  }

  const history = miniButton(`全部 ${bucket.versions.length}`);
  history.disabled = bucket.versions.length === 0;
  history.addEventListener("click", () => openHistory(batch, fileType));
  actions.appendChild(history);

  if (canUpload(fileType)) {
    const upload = miniButton("上传");
    upload.addEventListener("click", () => uploadFile(batch, fileType));
    actions.appendChild(upload);
  }

  td.append(name, actions);
  return td;
}

function miniButton(text) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "mini-btn";
  btn.textContent = text;
  return btn;
}

function actionCell(batch) {
  const td = document.createElement("td");
  const del = miniButton("删除");
  del.classList.add("danger");
  del.addEventListener("click", async () => {
    if (!confirm(`确认删除批次 ${batch.batch_no}？数据会软删除，不会清空上传文件。`)) return;
    try {
      await api(`/api/batches/${batch.id}`, { method: "DELETE" });
      state.batches = state.batches.filter((item) => item.id !== batch.id);
      renderAll();
      showToast("批次已删除");
    } catch (err) {
      showToast(err.message);
    }
  });
  td.appendChild(del);
  return td;
}

async function uploadFile(batch, fileType) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = fileAccepts[fileType] || ".xlsx,.xls,.xlsm,.csv";
  input.multiple = multiFileTypes.has(fileType);
  input.addEventListener("change", async () => {
    if (!input.files?.length) return;
    const form = new FormData();
    form.append("file_type", fileType);
    Array.from(input.files).forEach((file) => form.append("file", file));
    try {
      const payload = await api(`/api/batches/${batch.id}/files`, {
        method: "POST",
        body: form,
      });
      replaceBatch(payload.batch);
      renderAll();
      showToast(input.files.length > 1 ? "多个文件已上传" : "文件已上传");
    } catch (err) {
      showToast(err.message);
    }
  });
  input.click();
}

function openHistory(batch, fileType) {
  const bucket = batch.files[fileType] || { versions: [] };
  $("#historyTitle").textContent = `${batch.batch_no} · ${fileLabels[fileType]}历史版本`;
  const list = $("#historyList");
  list.innerHTML = "";
  bucket.versions.forEach((file) => {
    const item = document.createElement("div");
    item.className = "history-item";
    item.innerHTML = `
      <div>
        <strong>${escapeHtml(file.original_name)}</strong>
        <span>${escapeHtml(file.uploaded_by)} · ${shortTime(file.uploaded_at)} · ${formatSize(file.size_bytes)}</span>
      </div>
    `;
    const btn = miniButton("下载");
    btn.addEventListener("click", () => {
      window.location.href = `/api/files/${file.id}/download`;
    });
    item.appendChild(btn);
    list.appendChild(item);
  });
  if (bucket.versions.length === 0) {
    list.innerHTML = `<div class="empty">暂无历史版本</div>`;
  }
  $("#historyDialog").showModal();
}

function renderGantt() {
  const body = $("#ganttBody");
  body.innerHTML = "";
  if (state.batches.length === 0) {
    body.innerHTML = `<div class="empty">暂无批次</div>`;
    return;
  }

  const dates = [];
  state.batches.forEach((batch) => {
    ["synthesis_submitted_date", "synthesis_completed_date", "bio_test_start_date", "bio_test_completed_date"].forEach((field) => {
      if (batch[field]) dates.push(toDate(batch[field]));
    });
  });
  dates.push(new Date());
  const min = new Date(Math.min(...dates.map((d) => d.getTime())));
  const max = new Date(Math.max(...dates.map((d) => d.getTime())));
  min.setDate(min.getDate() - 2);
  max.setDate(max.getDate() + 5);
  const totalDays = Math.max(1, daysBetween(min, max));

  const grid = document.createElement("div");
  grid.className = "gantt-grid";
  const scale = document.createElement("div");
  scale.className = "gantt-scale";
  const markers = monthMarkers(min, max, totalDays);
  scale.innerHTML = `<span></span><div class="scale-track">${markers.map((marker) => (
    `<span class="month-marker" style="left:${marker.left}%">${marker.label}</span>`
  )).join("")}</div>`;
  grid.appendChild(scale);

  state.batches.forEach((batch) => {
    const row = document.createElement("div");
    row.className = "gantt-row";
    const meta = getStatus(batch);
    const label = document.createElement("div");
    label.className = "gantt-label";
    label.innerHTML = `<strong>${escapeHtml(batch.batch_no)}</strong><span>${escapeHtml(batch.project_name)} · ${meta.label}</span>`;
    const timeline = document.createElement("div");
    timeline.className = "timeline";
    addBar(timeline, min, totalDays, batch.synthesis_submitted_date, batch.synthesis_completed_date, "synthesis");
    addBar(timeline, min, totalDays, batch.bio_test_start_date, batch.bio_test_completed_date, "testing");
    const today = document.createElement("div");
    today.className = "today-line";
    today.style.left = `${percent(daysBetween(min, new Date()), totalDays)}%`;
    timeline.appendChild(today);
    row.append(label, timeline);
    grid.appendChild(row);
  });
  body.appendChild(grid);
}

function addBar(container, min, totalDays, start, end, className) {
  if (!start) return;
  const startDate = toDate(start);
  const endDate = end ? toDate(end) : new Date();
  const left = percent(daysBetween(min, startDate), totalDays);
  const width = Math.max(1.5, percent(Math.max(1, daysBetween(startDate, endDate) + 1), totalDays));
  const bar = document.createElement("div");
  bar.className = `bar ${className}`;
  bar.style.left = `${left}%`;
  bar.style.width = `${width}%`;
  container.appendChild(bar);
}

function percent(value, total) {
  return Math.max(0, Math.min(100, (value / total) * 100));
}

function toDate(value) {
  const [y, m, d] = value.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function daysBetween(a, b) {
  const day = 24 * 60 * 60 * 1000;
  const aa = new Date(a.getFullYear(), a.getMonth(), a.getDate());
  const bb = new Date(b.getFullYear(), b.getMonth(), b.getDate());
  return Math.round((bb - aa) / day);
}

function dateText(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function monthMarkers(min, max, totalDays) {
  const markers = [{ left: 0, label: monthLabel(min) }];
  let cursor = new Date(min.getFullYear(), min.getMonth() + 1, 1);
  while (cursor < max) {
    markers.push({
      left: percent(daysBetween(min, cursor), totalDays),
      label: monthLabel(cursor),
    });
    cursor = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
  }
  const lastLabel = markers[markers.length - 1].label;
  const maxLabel = monthLabel(max);
  if (maxLabel !== lastLabel) {
    markers.push({ left: 100, label: maxLabel });
  }
  return markers;
}

function monthLabel(date) {
  const month = `${date.getMonth() + 1}月`;
  if (date.getMonth() === 0) return `${date.getFullYear()}年${month}`;
  return month;
}

function replaceBatch(batch) {
  const idx = state.batches.findIndex((item) => item.id === batch.id);
  if (idx >= 0) {
    state.batches[idx] = batch;
  } else {
    state.batches.unshift(batch);
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function bindEvents() {
  $("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api("/api/login", {
        method: "POST",
        body: {
          username: form.get("username"),
          password: form.get("password"),
        },
      });
      await boot();
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#logoutBtn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    location.reload();
  });

  $("#refreshBtn").addEventListener("click", async () => {
    await loadData();
    showToast("已刷新");
  });

  $("#allProjectsBtn").addEventListener("click", async () => {
    state.selectedProjectId = "all";
    await loadData();
  });

  $("#tableTab").addEventListener("click", () => {
    state.view = "table";
    renderHeader();
  });

  $("#ganttTab").addEventListener("click", () => {
    state.view = "gantt";
    renderHeader();
  });

  $("#newProjectBtn").addEventListener("click", () => {
    $("#projectForm").reset();
    $("#projectDialog").showModal();
  });

  $("#projectForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api("/api/projects", {
        method: "POST",
        body: { name: form.get("name") },
      });
      $("#projectDialog").close();
      await loadData();
      showToast("项目已创建");
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#newBatchBtn").addEventListener("click", () => {
    if (state.projects.length === 0) {
      showToast("请先创建项目");
      return;
    }
    const select = $("#batchForm select[name='project_id']");
    select.innerHTML = state.projects.map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
    if (state.selectedProjectId !== "all") select.value = state.selectedProjectId;
    $("#batchForm").reset();
    if (state.selectedProjectId !== "all") select.value = state.selectedProjectId;
    $("#batchDialog").showModal();
  });

  $("#batchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const payload = await api("/api/batches", {
        method: "POST",
        body: {
          project_id: form.get("project_id"),
          batch_no: form.get("batch_no"),
          name: form.get("name"),
        },
      });
      $("#batchDialog").close();
      if (state.selectedProjectId === "all" || String(payload.batch.project_id) === String(state.selectedProjectId)) {
        replaceBatch(payload.batch);
      }
      renderAll();
      showToast("批次已创建");
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#deleteProjectBtn").addEventListener("click", async () => {
    const project = state.projects.find((item) => String(item.id) === String(state.selectedProjectId));
    if (!project) return;
    if (!confirm(`确认删除项目 ${project.name}？项目下批次会一起软删除。`)) return;
    try {
      await api(`/api/projects/${project.id}`, { method: "DELETE" });
      state.selectedProjectId = "all";
      await loadData();
      showToast("项目已删除");
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#passwordBtn").addEventListener("click", () => {
    $("#passwordForm").reset();
    $("#passwordDialog").showModal();
  });

  $("#passwordForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await api("/api/change-password", {
        method: "POST",
        body: {
          old_password: form.get("old_password"),
          new_password: form.get("new_password"),
        },
      });
      $("#passwordDialog").close();
      showToast("密码已修改");
    } catch (err) {
      showToast(err.message);
    }
  });

  $("#usersBtn").addEventListener("click", openUsers);
  $("#trashBtn").addEventListener("click", openTrash);

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => btn.closest("dialog").close());
  });
}

async function openUsers() {
  try {
    const payload = await api("/api/users");
    const list = $("#usersList");
    list.innerHTML = "";
    payload.users.forEach((user) => {
      const row = document.createElement("div");
      row.className = "user-row";
      row.innerHTML = `
        <div>
          <strong>${escapeHtml(user.display_name)}</strong>
          <span>${escapeHtml(user.username)} · ${roleLabel(user.role)}</span>
        </div>
      `;
      const form = document.createElement("form");
      form.className = "reset-form";
      form.innerHTML = `<input name="password" type="password" minlength="6" placeholder="新密码"><button class="mini-btn" type="submit">重置</button>`;
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const password = new FormData(form).get("password");
        try {
          await api("/api/reset-password", {
            method: "POST",
            body: { user_id: user.id, new_password: password },
          });
          form.reset();
          showToast("密码已重置");
        } catch (err) {
          showToast(err.message);
        }
      });
      row.appendChild(form);
      list.appendChild(row);
    });
    $("#usersDialog").showModal();
  } catch (err) {
    showToast(err.message);
  }
}

async function openTrash() {
  try {
    const payload = await api("/api/trash");
    renderTrash(payload);
    if (!$("#trashDialog").open) $("#trashDialog").showModal();
  } catch (err) {
    showToast(err.message);
  }
}

function renderTrash(payload) {
  const projects = $("#trashProjects");
  const batches = $("#trashBatches");
  projects.innerHTML = "";
  batches.innerHTML = "";

  if (payload.projects.length === 0) {
    projects.appendChild(trashEmpty("没有已删除项目"));
  } else {
    payload.projects.forEach((project) => {
      const item = document.createElement("div");
      item.className = "trash-item";
      item.innerHTML = `
        <div>
          <strong>${escapeHtml(project.name)}</strong>
          <span>删除时间：${shortTime(project.deleted_at)}</span>
        </div>
      `;
      const btn = miniButton("恢复项目");
      btn.addEventListener("click", () => restoreProject(project.id));
      item.appendChild(btn);
      projects.appendChild(item);
    });
  }

  if (payload.batches.length === 0) {
    batches.appendChild(trashEmpty("没有已删除批次"));
  } else {
    payload.batches.forEach((batch) => {
      const item = document.createElement("div");
      item.className = "trash-item";
      const projectState = batch.project_deleted_at ? "所属项目也在回收站" : "所属项目可用";
      item.innerHTML = `
        <div>
          <strong>${escapeHtml(batch.name)} · ${escapeHtml(batch.batch_no)}</strong>
          <span>${escapeHtml(batch.project_name)} · ${projectState} · 删除时间：${shortTime(batch.deleted_at)}</span>
        </div>
      `;
      const btn = miniButton(batch.project_deleted_at ? "先恢复项目" : "恢复批次");
      btn.disabled = Boolean(batch.project_deleted_at);
      btn.addEventListener("click", () => restoreBatch(batch.id));
      item.appendChild(btn);
      batches.appendChild(item);
    });
  }
}

function trashEmpty(text) {
  const item = document.createElement("div");
  item.className = "empty";
  item.textContent = text;
  return item;
}

async function restoreProject(projectId) {
  try {
    await api(`/api/projects/${projectId}/restore`, { method: "POST" });
    await loadData();
    await openTrash();
    showToast("项目已恢复");
  } catch (err) {
    showToast(err.message);
  }
}

async function restoreBatch(batchId) {
  try {
    await api(`/api/batches/${batchId}/restore`, { method: "POST" });
    await loadData();
    await openTrash();
    showToast("批次已恢复");
  } catch (err) {
    showToast(err.message);
  }
}

bindEvents();
boot();
