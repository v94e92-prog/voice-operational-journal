"use strict";

const $ = (s) => document.querySelector(s);
let PIN = sessionStorage.getItem("adminPin") || "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Admin-Pin": PIN, ...(opts.headers || {}) } });
  let data = null;
  try { data = await res.json(); } catch (_) { /* пусто */ }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || `Ошибка ${res.status}`);
  }
  return data;
}
let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 5000);
}
const fmt = (iso) => (iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)} ${iso.slice(11, 16)}` : "—");

const STATUS = {
  unchanged: ["ok", "Без изменений"],
  changed: ["warn", "Есть изменения"],
  first: ["warn", "Нужно принять"],
  error: ["err", "Не удалось"],
  no_url: ["none", "Нет ссылки"],
};

// ------------------------------------------------------------ вход
async function login(pin) {
  PIN = pin;
  const me = await api("/api/admin/login", { method: "POST" });
  sessionStorage.setItem("adminPin", pin);
  $("#adminLine").textContent = `Администратор: ${me.fio}, ${me.position.toLowerCase()}`;
  $("#loginCard").classList.add("hidden");
  $("#adminArea").classList.remove("hidden");
  $("#btnLogout").classList.remove("hidden");
  await Promise.all([loadDocs(), loadTemplates(), loadUsers(), loadBackups(), loadAudit()]);
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await login($("#adminPin").value); } catch (err) { $("#loginErr").textContent = err.message; PIN = ""; }
});
$("#btnLogout").onclick = () => { sessionStorage.removeItem("adminPin"); location.reload(); };

// ------------------------------------------------------------ документы
async function loadDocs() {
  const docs = await api("/api/admin/docs");
  $("#docRows").innerHTML = docs.map((d) => {
    const [cls, label] = STATUS[d.last_status] || ["none", "Не проверялся"];
    return `<tr data-code="${esc(d.code)}">
      <td><b>${esc(d.title)}</b><div class="muted">${d.saved_at ? `Принятая редакция: ${fmt(d.saved_at)}, ${esc(d.saved_by)}` : "Редакция ещё не принята"}</div></td>
      <td><input type="text" class="doc-url" value="${esc(d.url)}" placeholder="https://… или C:\\путь\\к\\файлу.html">
          <details style="margin-top:6px"><summary class="muted">Маркеры начала и конца текста</summary>
            <div style="display:grid; gap:6px; margin-top:6px">
              <input type="text" class="doc-start" value="${esc(d.start_marker)}" placeholder="Текст, с которого начинается документ">
              <input type="text" class="doc-end" value="${esc(d.end_marker)}" placeholder="Текст, которым документ заканчивается">
            </div></details>
          <button class="btn small doc-save" type="button" style="margin-top:6px">Сохранить ссылку</button></td>
      <td><span class="badge ${cls}">${label}</span><div class="muted" style="margin-top:4px">${fmt(d.last_check_at)}</div>
          <div style="font-size:13px; margin-top:4px">${esc(d.last_message || "")}</div></td>
      <td><div class="btn-col">
          <button class="btn small doc-check" type="button">Проверить</button>
          ${d.pending_hash ? `<button class="btn small doc-diff" type="button">Показать текст и изменения</button>
          <button class="btn small primary doc-accept" type="button">Принять как текущую</button>` : ""}
      </div></td></tr>`;
  }).join("");
  document.querySelectorAll("#docRows tr").forEach((tr) => {
    const code = tr.dataset.code;
    tr.querySelector(".doc-check").onclick = (e) => checkDoc(code, e.target);
    tr.querySelector(".doc-save").onclick = () => saveDoc(code, tr);
    const diff = tr.querySelector(".doc-diff");
    if (diff) diff.onclick = () => showDiff(code, tr.querySelector("b").textContent);
    const acc = tr.querySelector(".doc-accept");
    if (acc) acc.onclick = () => acceptDoc(code);
  });
}

async function saveDoc(code, tr) {
  try {
    await api(`/api/admin/docs/${code}`, { method: "POST", body: JSON.stringify({
      url: tr.querySelector(".doc-url").value, start_marker: tr.querySelector(".doc-start").value, end_marker: tr.querySelector(".doc-end").value }) });
    toast("Ссылка сохранена.", "ok");
    await Promise.all([loadDocs(), loadAudit()]);
  } catch (e) { toast(e.message, "err"); }
}

async function checkDoc(code, btn) {
  btn.disabled = true;
  btn.textContent = "Проверяю…";
  try {
    const r = await api(`/api/admin/docs/${code}/check`, { method: "POST" });
    toast(r.message, r.status === "unchanged" ? "ok" : r.status === "error" || r.status === "no_url" ? "err" : "warn");
    await loadDocs();
    if (r.status === "changed" || r.status === "first") {
      const title = document.querySelector(`#docRows tr[data-code="${code}"] b`).textContent;
      await showDiff(code, title);
    }
  } catch (e) { toast(e.message, "err"); btn.disabled = false; btn.textContent = "Проверить"; }
}

$("#btnCheckAll").onclick = async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Проверяю…";
  try {
    const res = await api("/api/admin/docs/check_all", { method: "POST" });
    const changed = res.filter((r) => r.status === "changed").length;
    const failed = res.filter((r) => r.status === "error").length;
    toast(`Проверено документов: ${res.length}. С изменениями: ${changed}. Не удалось проверить: ${failed}.`, changed || failed ? "warn" : "ok");
    await loadDocs();
  } catch (err) { toast(err.message, "err"); }
  e.target.disabled = false;
  e.target.textContent = "Проверить все";
};

async function showDiff(code, title) {
  const d = await api(`/api/admin/docs/${code}/diff`);
  $("#diffTitle").textContent = title;
  $("#diffRedactions").innerHTML = d.redactions.length
    ? `<div class="note info" style="margin-bottom:10px"><b>Сведения о редакциях в тексте:</b><br>${d.redactions.map(esc).join("<br>")}</div>` : "";
  if (d.diff.length) {
    $("#diffBody").innerHTML = d.diff.map((l) => {
      const cls = { "+": "add", "-": "del", "@": "hunk", " ": "ctx" }[l.op] || "ctx";
      return `<span class="${cls}">${l.op === "+" ? "+ " : l.op === "-" ? "− " : ""}${esc(l.text)}</span>`;
    }).join("");
  } else {
    $("#diffBody").innerHTML = `<span class="ctx">${esc(d.preview)}${d.preview.length >= 3000 ? "\n…" : ""}</span>`;
  }
  $("#diffPanel").classList.remove("hidden");
  $("#diffPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("#btnDiffClose").onclick = () => $("#diffPanel").classList.add("hidden");

async function acceptDoc(code) {
  if (!confirm("Принять скачанный текст как текущую редакцию? Действие будет записано в журнал действий.")) return;
  try {
    await api(`/api/admin/docs/${code}/accept`, { method: "POST" });
    toast("Редакция принята.", "ok");
    $("#diffPanel").classList.add("hidden");
    await Promise.all([loadDocs(), loadAudit()]);
  } catch (e) { toast(e.message, "err"); }
}

// ------------------------------------------------------------ шаблоны
async function loadTemplates() {
  const { templates } = await api("/api/admin/templates");
  $("#tplList").innerHTML = templates.map((t) => `
    <div class="tpl">
      <div style="display:flex; justify-content:space-between; gap:10px; align-items:baseline">
        <b style="font-size:16px">${esc(t.name)}</b>
        ${t.status === "modified"
          ? `<span class="badge err">Файл формы изменён без смены версии!</span>`
          : `<span class="badge ok">Версия ${esc(t.version)} зарегистрирована</span>`}
      </div>
      <dl>
        <dt>Версия формы</dt><dd>${esc(t.version)}, действует с ${esc(t.effective_from)}</dd>
        <dt>Утверждена</dt><dd>${esc(t.approved_by_local_act)}</dd>
        <dt>Документы-основания</dt><dd>${t.basis_titles.map(esc).join("<br>")}</dd>
        <dt>Графы</dt><dd>${t.columns.map((c) => esc(c.title)).join(" | ")}</dd>
        ${t.note ? `<dt>Примечание</dt><dd>${esc(t.note)}</dd>` : ""}
      </dl>
    </div>`).join("");
}

// ------------------------------------------------------------ работники
async function loadUsers() {
  const users = await api("/api/admin/users");
  $("#userRows").innerHTML = users.map((u) => `<tr><td>${esc(u.fio)}</td><td>${esc(u.position)}</td><td>${u.role === "admin" ? "Администратор" : "Дежурный"}</td></tr>`).join("");
}
$("#userForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/admin/users", { method: "POST", body: JSON.stringify({ fio: $("#uFio").value, position: $("#uPos").value, role: $("#uRole").value, pin: $("#uPin").value }) });
    e.target.reset();
    toast("Работник добавлен.", "ok");
    await Promise.all([loadUsers(), loadAudit()]);
  } catch (err) { toast(err.message, "err"); }
});

// ------------------------------------------------------------ копии и аудит
async function loadBackups() {
  const list = await api("/api/admin/backups");
  $("#backupRows").innerHTML = list.length
    ? list.map((b) => `<tr><td><code>${esc(b.name)}</code></td><td>${fmt(b.created)}</td><td>${(b.size / 1024).toFixed(0)} КБ</td></tr>`).join("")
    : `<tr><td colspan="3" class="muted">Копий пока нет. Файлы копий лежат в папке data\\backups.</td></tr>`;
}
$("#btnBackup").onclick = async () => {
  try {
    const b = await api("/api/admin/backup", { method: "POST" });
    toast(`Создана копия ${b.name}.`, "ok");
    await Promise.all([loadBackups(), loadAudit()]);
  } catch (e) { toast(e.message, "err"); }
};

async function loadAudit() {
  const list = await api("/api/admin/audit");
  $("#auditRows").innerHTML = list.map((a) => `<tr><td>${fmt(a.at)}</td><td>${esc(a.who)}</td><td>${esc(a.action)}${a.details ? ` — <span class="muted">${esc(a.details)}</span>` : ""}</td></tr>`).join("");
}

if (PIN) login(PIN).catch(() => { sessionStorage.removeItem("adminPin"); PIN = ""; });
