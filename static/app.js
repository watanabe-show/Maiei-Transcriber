"use strict";

const $ = (id) => document.getElementById(id);

const drop = $("drop");
const fileInput = $("fileInput");
const fileBox = $("fileBox");
const uploadPanel = $("uploadPanel");
const progressPanel = $("progressPanel");
const resultPanel = $("resultPanel");

let currentJobId = null;
let views = {};           // { gran: [{start,end,text}] }  各切り方ごとのブロック
let pollTimer = null;
let currentGran = "sec10";   // 選択中の切り方（プルダウンと連動）

// ---------------------------------------------------------------- utils
function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  setTimeout(() => (t.className = "toast"), 3400);
}
function fmtSize(b) {
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(0) + " KB";
  return (b / 1048576).toFixed(1) + " MB";
}
function fmtTs(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
const show = (el) => el.classList.remove("hidden");
const hide = (el) => el.classList.add("hidden");

// ---------------------------------------------------------------- file pick
drop.addEventListener("click", () => fileInput.click());
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
drop.addEventListener("drop", (e) => {
  e.preventDefault(); drop.classList.remove("drag");
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) handleFile(fileInput.files[0]);
});

function handleFile(file) {
  $("fileName").textContent = file.name;
  $("fileSize").textContent = fmtSize(file.size);
  show(fileBox);
  startTranscription(file);
}

// ---------------------------------------------------------------- transcribe
async function startTranscription(file) {
  stopPolling();
  hide(resultPanel);
  show(progressPanel);
  progressPanel.classList.remove("err-box");
  $("progressTitle").textContent = "アップロード中…";
  setProgress(2, "ファイルを送信しています…");

  const body = new FormData();
  body.append("file", file);
  body.append("language", $("language").value);

  try {
    const res = await fetch("/api/transcribe", { method: "POST", body });
    if (res.status === 401) { location.href = "/"; return; }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      return fail(data.detail || "アップロードに失敗しました。");
    }
    const data = await res.json();
    currentJobId = data.job_id;
    $("progressTitle").textContent = "処理中…";
    poll();
  } catch (_) { fail("サーバーに接続できませんでした。"); }
}

function poll() { pollTimer = setInterval(checkStatus, 1300); checkStatus(); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

async function checkStatus() {
  if (!currentJobId) return;
  try {
    const res = await fetch(`/api/jobs/${currentJobId}`);
    if (res.status === 401) { location.href = "/"; return; }
    if (!res.ok) return;
    const job = await res.json();
    setProgress(job.progress || 0, job.stage || "");
    if (job.status === "done") { stopPolling(); renderResult(job); }
    else if (job.status === "error") { stopPolling(); fail(job.error || "処理に失敗しました。"); }
  } catch (_) { /* 一時的失敗は次のpollで再試行 */ }
}

function setProgress(pct, stage) {
  $("bar").style.width = Math.min(100, Math.max(0, pct)) + "%";
  $("progressPct").textContent = Math.round(pct) + "%";
  if (stage) $("stage").textContent = stage;
}

function fail(msg) {
  stopPolling();
  show(progressPanel);
  progressPanel.classList.add("err-box");
  $("progressTitle").textContent = "エラー";
  $("stage").textContent = msg;
  toast("エラーが発生しました", "err");
}

// ---------------------------------------------------------------- result
function renderResult(job) {
  views = job.views || {};
  // 万一 views が無くても text だけは出せるようにする
  if ((!views || !Object.keys(views).length) && job.text) {
    views = { plain: [{ start: 0, end: 0, text: job.text }] };
  }
  hide(progressPanel);
  show(resultPanel);
  currentGran = $("gran").value || "sec10";
  renderView();
}

// 選択中の切り方のブロック一覧を返す
function currentList() {
  return (views[currentGran] && views[currentGran].length)
    ? views[currentGran]
    : (views.plain || []);
}

function renderView() {
  const box = $("transcript");
  box.innerHTML = "";
  const list = currentList();
  if (!list.length) {
    box.textContent = "（テキストを検出できませんでした）";
    return;
  }
  const showTc = currentGran !== "plain";
  for (const b of list) {
    const div = document.createElement("div");
    div.className = "para";
    if (showTc) {
      const tc = document.createElement("span");
      tc.className = "tc";
      tc.textContent = `[${fmtTs(b.start)}]`;
      div.appendChild(tc);
    }
    const tx = document.createElement("span");
    tx.className = "tx";
    tx.textContent = b.text;
    div.appendChild(tx);
    box.appendChild(div);
  }
}

$("gran").addEventListener("change", () => {
  currentGran = $("gran").value;
  renderView();
});

$("copyBtn").addEventListener("click", async () => {
  const list = currentList();
  const text = currentGran === "plain"
    ? list.map((p) => p.text).join("\n\n")
    : list.map((b) => `[${fmtTs(b.start)}] ${b.text}`).join("\n");
  try { await navigator.clipboard.writeText(text); toast("コピーしました"); }
  catch (_) { toast("コピーに失敗しました", "err"); }
});

function download(fmt) {
  if (!currentJobId) return;
  // テキスト・Word・Excel は選択中の切り方で保存（字幕srtは切り方の影響なし）
  window.location.href =
    `/api/jobs/${currentJobId}/download?fmt=${fmt}&gran=${encodeURIComponent(currentGran)}`;
}
$("dlTxt").addEventListener("click", () => download("txt"));
$("dlDocx").addEventListener("click", () => download("docx"));
$("dlXlsx").addEventListener("click", () => download("xlsx"));
$("dlSrt").addEventListener("click", () => download("srt"));

$("againBtn").addEventListener("click", () => {
  hide(resultPanel); hide(fileBox);
  fileInput.value = "";
  currentJobId = null; views = {};
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// ---------------------------------------------------------------- recording
let mediaRecorder = null, recChunks = [];
$("recordBtn").addEventListener("click", async () => {
  const btn = $("recordBtn");
  if (mediaRecorder && mediaRecorder.state === "recording") { mediaRecorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      btn.textContent = "● マイクで録音";
      btn.classList.remove("btn-stamp");
      const file = new File([new Blob(recChunks, { type: "audio/webm" })], "recording.webm", { type: "audio/webm" });
      handleFile(file);
    };
    mediaRecorder.start();
    btn.textContent = "■ 録音停止";
    btn.classList.add("btn-stamp");
  } catch (_) { toast("マイクを使用できませんでした", "err"); }
});

// ---------------------------------------------------------------- logout
$("logoutBtn").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/";
});
