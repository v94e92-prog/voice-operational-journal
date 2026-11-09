"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  status: null, users: [], entries: [], kinds: {},
  rec: null, stream: null, chunks: [], t0: 0, timer: null, autoStop: null,
  raw: "", kindManual: false, parseTimer: null, correcting: null,
};

// ------------------------------------------------------------ вспомогательное
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function errText(data, res) {
  const d = data && data.detail;
  if (Array.isArray(d)) return d.map((x) => x.msg).join("; ");
  return d || `Ошибка ${res.status}`;
}
async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  let data = null;
  try { data = await res.json(); } catch (_) { /* пустой ответ */ }
  if (!res.ok) throw new Error(errText(data, res));
  return data;
}
let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), kind === "err" ? 7000 : 4000);
}
const fmtDate = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;
const fmtTime = (iso) => iso.slice(11, 16);

// ------------------------------------------------------------ состояние программы
async function loadStatus() {
  state.status = await api("/api/status");
  const s = state.status;
  $("#journalName").textContent = `${s.journal.name} · форма v${s.journal.version}`;
  $("#orgLine").textContent = [s.organization, s.department].filter(Boolean).join(" · ");
  state.kinds = Object.fromEntries(s.journal.kinds.map((k) => [k.code, k.label]));
  const sel = $("#kindSel");
  if (!sel.options.length) {
    for (const k of s.journal.kinds.filter((k) => !k.system)) sel.add(new Option(k.label, k.code));
  }
  $("#entryPin").classList.toggle("hidden", !s.journal.rules.require_pin_per_entry);
  renderShift();
  renderAsr();
  updateSaveButton();
  if (s.asr.loading) setTimeout(loadStatus, 3000);
}

function renderAsr() {
  const a = state.status.asr;
  const mic = $("#btnMic");
  if (state.rec) return;
  mic.classList.toggle("disabled", !a.ready);
  if (!a.ready) {
    $("#micStatus").textContent = a.loading ? "Загружается распознавание…" : "Голосовой ввод недоступен";
    $("#asrInfo").textContent = a.message;
    if (a.error) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn small";
      b.style.marginTop = "6px";
      b.textContent = "Повторить загрузку";
      b.onclick = async () => { await api("/api/asr/retry", { method: "POST" }); await loadStatus(); };
      $("#asrInfo").append(document.createElement("br"), b);
    }
  } else {
    $("#micStatus").textContent = "Нажмите и говорите";
    $("#asrInfo").textContent = `Нажмите ещё раз, чтобы закончить (до ${a.max_seconds} с). Клавиша F2 — то же самое.`;
  }
}

function renderShift() {
  const sh = state.status.shift;
  const bar = $("#shiftBar");
  if (!sh) {
    bar.innerHTML = `<div class="shift-info"><span class="dot off"></span><span><b>Смена не принята.</b> Вносить записи нельзя.</span></div>
      <button class="btn primary" type="button" id="btnAccept">Принять смену</button>`;
    $("#btnAccept").onclick = openAccept;
  } else {
    bar.innerHTML = `<div class="shift-info"><span class="dot"></span><span>Дежурный: <b>${esc(sh.fio)}</b>${sh.position ? ", " + esc(sh.position.toLowerCase()) : ""} · смена с ${fmtTime(sh.started_at)} ${fmtDate(sh.started_at)}</span></div>
      <button class="btn" type="button" id="btnHandover">Сдать смену</button>`;
    $("#btnHandover").onclick = openHandover;
  }
}

// ------------------------------------------------------------ журнал
async function loadEntries() {
  state.entries = await api("/api/entries?limit=300");
  renderEntries();
}

function renderEntries() {
  const tb = $("#rows");
  const sh = state.status.shift;
  tb.innerHTML = state.entries.map((e) => {
    const canFix = sh && e.shift_id === sh.id && !e.erroneous_by && e.kind !== "shift";
    const text = e.erroneous_by
      ? `<span class="err-text">(${esc(e.text)})</span><div class="err-note">Ошибочна — см. запись № ${e.erroneous_by}</div>`
      : esc(e.text);
    const sig = `<span>${esc(e.author_fio)}</span>` + (e.cosigner_fio ? `<span>${esc(e.cosigner_fio)}</span>` : "");
    return `<tr class="${e.kind}">
      <td class="num">${e.id}</td><td class="date">${fmtDate(e.created_at)}</td><td class="time">${fmtTime(e.created_at)}</td>
      <td class="text">${text}<div class="kind">${esc(state.kinds[e.kind] || e.kind)}</div></td>
      <td class="sig">${sig}</td>
      <td>${canFix ? `<button class="btn small danger-ghost" data-fix="${e.id}" type="button">Ошибочна</button>` : ""}</td></tr>`;
  }).join("");
  $("#emptyLine").classList.toggle("hidden", state.entries.length > 0);
  $("#countLine").textContent = state.entries.length ? `показано записей: ${state.entries.length}` : "";
  tb.querySelectorAll("[data-fix]").forEach((b) => (b.onclick = () => startCorrection(+b.dataset.fix)));
  const wrap = $("#tableWrap");
  wrap.scrollTop = wrap.scrollHeight;
}

// ------------------------------------------------------------ голос
async function toggleMic() {
  if (state.rec) return stopRec();
  const a = state.status.asr;
  if (!a.ready) return toast(a.message, "warn");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return toast("Браузер не даёт доступ к микрофону. Откройте программу по адресу http://localhost:8000 на этом же компьютере.", "err");
  }
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) {
    return toast("Нет доступа к микрофону: " + e.message, "err");
  }
  const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"].find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
  const rec = new MediaRecorder(state.stream, mime ? { mimeType: mime } : undefined);
  state.chunks = [];
  rec.ondataavailable = (e) => e.data && e.data.size && state.chunks.push(e.data);
  rec.onstop = () => {
    state.stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(state.chunks, { type: rec.mimeType || "audio/webm" });
    sendAudio(blob);
  };
  rec.start(250);
  state.rec = rec;
  state.t0 = Date.now();
  $("#btnMic").className = "mic rec";
  $("#btnMic").setAttribute("aria-label", "Закончить запись");
  $("#micStatus").textContent = "Идёт запись — говорите";
  state.timer = setInterval(tick, 200);
  state.autoStop = setTimeout(() => state.rec && stopRec(), a.max_seconds * 1000);
}

function tick() {
  const s = Math.floor((Date.now() - state.t0) / 1000);
  $("#micTimer").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function stopRec() {
  clearInterval(state.timer);
  clearTimeout(state.autoStop);
  const rec = state.rec;
  state.rec = null;
  $("#btnMic").className = "mic busy";
  $("#btnMic").setAttribute("aria-label", "Начать запись голоса");
  $("#micStatus").textContent = "Распознаю…";
  rec.stop();
}

async function sendAudio(blob) {
  const fd = new FormData();
  const ext = blob.type.includes("ogg") ? "ogg" : blob.type.includes("mp4") ? "m4a" : "webm";
  fd.append("audio", blob, "speech." + ext);
  try {
    const res = await fetch("/api/transcribe", { method: "POST", body: fd });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error(errText(data, res));
    const ta = $("#entryText");
    ta.value = ta.value.trim() ? ta.value.trim() + " " + data.parsed.text : data.parsed.text;
    state.raw = state.raw ? state.raw + " | " + data.raw : data.raw;
    await analyze(data.parsed.replacements);
  } catch (e) {
    toast(e.message, "err");
  } finally {
    $("#btnMic").className = "mic";
    $("#micTimer").textContent = "0:00";
    renderAsr();
  }
}

// ------------------------------------------------------------ разбор текста
async function analyze(replacements = null) {
  const text = $("#entryText").value.trim();
  updateSaveButton();
  if (!text) return renderPreview(null, null);
  try {
    const p = await api("/api/parse", { method: "POST", body: JSON.stringify({ text, normalize: false }) });
    renderPreview(p, replacements);
  } catch (e) { /* разбор — подсказка, не мешаем вводу */ }
}

function renderPreview(p, replacements) {
  const notes = $("#notes");
  if (!p) {
    $("#equipRow").classList.add("hidden");
    notes.innerHTML = "";
    return;
  }
  if (!state.kindManual && !state.correcting) $("#kindSel").value = p.kind;
  const eq = p.fields.equipment || [];
  const extra = [];
  if (p.fields.naryad) extra.push(`<span class="chip neutral">наряд <b>№ ${esc(p.fields.naryad)}</b></span>`);
  if (p.fields.order_no) extra.push(`<span class="chip neutral">распоряжение <b>№ ${esc(p.fields.order_no)}</b></span>`);
  if (p.fields.blank) extra.push(`<span class="chip neutral">бланк <b>№ ${esc(p.fields.blank)}</b></span>`);
  if (p.fields.time_mentioned) extra.push(`<span class="chip neutral">время в тексте <b>${esc(p.fields.time_mentioned)}</b></span>`);
  $("#equipChips").innerHTML = eq.map((e) => `<span class="chip"><b>${esc(e.code)}</b> ${esc(e.name)}</span>`).join("") + extra.join("");
  $("#equipRow").classList.toggle("hidden", !eq.length && !extra.length);
  let html = "";
  if (replacements && replacements.length) {
    html += `<div class="note info">Исправлено по справочнику: ${replacements.map((r) => `«${esc(r.from)}» → ${esc(r.to)}`).join(", ")}. Проверьте текст перед внесением.</div>`;
  }
  for (const w of p.warnings) html += `<div class="note warn">${esc(w)}</div>`;
  notes.innerHTML = html;
}

function updateSaveButton() {
  const ok = !!(state.status && state.status.shift) && $("#entryText").value.trim().length > 0;
  $("#btnSave").disabled = !ok;
  $("#btnSave").textContent = state.correcting ? "Внести исправление" : "Внести в журнал";
  $("#btnSave").title = state.status && !state.status.shift ? "Сначала примите смену" : "";
}

// ------------------------------------------------------------ внесение записи
async function save() {
  const text = $("#entryText").value.trim();
  if (!text) return;
  const missed = $("#chkMissed").checked;
  const body = {
    text, kind: $("#kindSel").value, raw_transcript: state.raw || null, missed,
    event_time: missed ? $("#missedTime").value || null : null,
    corrects_id: state.correcting ? state.correcting.id : null,
    pin: $("#entryPin").value || null,
  };
  if (missed && !body.event_time) return toast("Укажите время события для пропущенной записи.", "warn");
  $("#btnSave").disabled = true;
  try {
    const e = await api("/api/entries", { method: "POST", body: JSON.stringify(body) });
    toast(`Запись № ${e.id} внесена в журнал.`, "ok");
    clearForm();
    await loadEntries();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    updateSaveButton();
  }
}

function clearForm() {
  $("#entryText").value = "";
  $("#chkMissed").checked = false;
  $("#missedTime").value = "";
  $("#missedTimeWrap").classList.add("hidden");
  $("#entryPin").value = "";
  state.raw = "";
  state.kindManual = false;
  cancelCorrection();
  renderPreview(null, null);
  updateSaveButton();
}

function startCorrection(id) {
  const e = state.entries.find((x) => x.id === id);
  if (!e) return;
  state.correcting = e;
  $("#corrText").innerHTML = `<b>Исправление записи № ${e.id}:</b> «${esc(e.text)}».<br>Продиктуйте или введите правильную запись. Ошибочная останется в журнале зачёркнутой.`;
  $("#corrBanner").classList.remove("hidden");
  $("#entryText").focus();
  updateSaveButton();
}

function cancelCorrection() {
  state.correcting = null;
  $("#corrBanner").classList.add("hidden");
  updateSaveButton();
}

// ------------------------------------------------------------ смена
function fillUsers(sel, excludeId) {
  sel.innerHTML = "";
  for (const u of state.users.filter((u) => u.id !== excludeId)) sel.add(new Option(`${u.fio} — ${u.position}`, u.id));
}

async function openAccept() {
  state.users = await api("/api/users");
  fillUsers($("#accUser"));
  $("#accPin").value = "";
  $("#accErr").textContent = "";
  $("#dlgAccept").showModal();
  $("#accPin").focus();
}

async function openHandover() {
  state.users = await api("/api/users");
  const sh = state.status.shift;
  $("#hoFrom").textContent = `Сдаёт: ${sh.fio}`;
  fillUsers($("#hoTo"), sh.duty_user_id);
  ["#hoFromPin", "#hoToPin", "#hoNote"].forEach((s) => ($(s).value = ""));
  $("#hoErr").textContent = "";
  $("#dlgHandover").showModal();
  $("#hoFromPin").focus();
}

$("#formAccept").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  try {
    await api("/api/shift/accept", { method: "POST", body: JSON.stringify({ user_id: +$("#accUser").value, pin: $("#accPin").value }) });
    $("#dlgAccept").close();
    toast("Смена принята.", "ok");
    await loadStatus();
    await loadEntries();
  } catch (e) { $("#accErr").textContent = e.message; }
});

$("#formHandover").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  try {
    await api("/api/shift/handover", {
      method: "POST",
      body: JSON.stringify({ from_pin: $("#hoFromPin").value, to_user_id: +$("#hoTo").value, to_pin: $("#hoToPin").value, note: $("#hoNote").value }),
    });
    $("#dlgHandover").close();
    toast("Смена передана.", "ok");
    clearForm();
    await loadStatus();
    await loadEntries();
  } catch (e) { $("#hoErr").textContent = e.message; }
});

// ------------------------------------------------------------ целостность
async function verify() {
  try {
    const r = await api("/api/verify");
    if (r.ok) toast(`Целостность подтверждена: ${r.count} записей, нумерация и цепочка подписей не нарушены.`, "ok");
    else toast(`ВНИМАНИЕ: ${r.reason}`, "err");
  } catch (e) { toast(e.message, "err"); }
}

// ------------------------------------------------------------ события
$("#btnMic").onclick = toggleMic;
$("#btnSave").onclick = save;
$("#btnClear").onclick = clearForm;
$("#btnCorrCancel").onclick = cancelCorrection;
$("#btnVerify").onclick = verify;
$("#kindSel").onchange = () => (state.kindManual = true);
$("#chkMissed").onchange = (e) => {
  $("#missedTimeWrap").classList.toggle("hidden", !e.target.checked);
  if (e.target.checked) $("#missedTime").focus();
};
$("#entryText").addEventListener("input", () => {
  updateSaveButton();
  clearTimeout(state.parseTimer);
  state.parseTimer = setTimeout(() => analyze(null), 450);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "F2") { e.preventDefault(); toggleMic(); }
  if (e.key === "Enter" && e.ctrlKey && !$("#btnSave").disabled) save();
});

(async () => {
  try {
    await loadStatus();
    await loadEntries();
  } catch (e) {
    toast("Сервер недоступен: " + e.message, "err");
  }
})();
