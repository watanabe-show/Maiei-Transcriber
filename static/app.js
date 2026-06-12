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

// サーバー設定（/api/config で上書き）。storage_enabled の時だけ大容量直アップ経路を使う。
let APP_CONFIG = { storage_enabled: false, direct_max_mb: 200, storage_max_mb: 3000, part_mb: 64 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    if (res.ok) APP_CONFIG = Object.assign(APP_CONFIG, await res.json());
  } catch (_) { /* 取得できなければ従来動作のまま */ }
})();

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

  // 大きいファイル かつ ストレージ有効時のみ、ブラウザ→R2へ直接アップロードする。
  const useStorage =
    APP_CONFIG.storage_enabled && file.size > APP_CONFIG.direct_max_mb * 1048576;

  try {
    const jobId = useStorage ? await uploadViaStorage(file) : await uploadDirect(file);
    if (!jobId) return;   // 失敗時は内部で fail() 済み（またはリダイレクト）
    currentJobId = jobId;
    $("progressTitle").textContent = "処理中…";
    poll();
  } catch (_) { fail("サーバーに接続できませんでした。"); }
}

// 従来どおりサーバー経由でアップロード（小さいファイル・マイク録音）
async function uploadDirect(file) {
  const body = new FormData();
  body.append("file", file);
  body.append("language", $("language").value);
  const res = await fetch("/api/transcribe", { method: "POST", body });
  if (res.status === 401) { location.href = "/"; return null; }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    fail(data.detail || "アップロードに失敗しました。");
    return null;
  }
  return (await res.json()).job_id;
}

// 大容量：R2へ multipart で直接アップロード → /api/transcribe-key でジョブ化
async function uploadViaStorage(file) {
  const initRes = await fetch("/api/uploads/initiate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, size: file.size }),
  });
  if (initRes.status === 401) { location.href = "/"; return null; }
  if (!initRes.ok) {
    const d = await initRes.json().catch(() => ({}));
    fail(d.detail || "アップロードの準備に失敗しました。");
    return null;
  }
  const info = await initRes.json();
  try {
    return await runMultipart(file, info);
  } catch (err) {
    // 後始末（サーバー側のライフサイクルでも掃除されるが念のため中断要求）
    try {
      await fetch("/api/uploads/abort", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: info.key, upload_id: info.upload_id }),
      });
    } catch (_) { /* noop */ }
    if (err && err.unauth) { location.href = "/"; return null; }
    fail((err && err.message) || "大容量アップロードに失敗しました。通信状況を確認してお試しください。");
    return null;
  }
}

async function runMultipart(file, info) {
  const { key, upload_id, part_size, part_count } = info;
  const partUrls = info.part_urls.slice();        // 失効時に差し替える
  const partLoaded = new Array(part_count).fill(0);
  const etags = new Array(part_count);

  const refresh = () => {
    const sum = partLoaded.reduce((a, b) => a + b, 0);
    setProgress(
      2 + Math.round(96 * sum / file.size),
      `アップロード中… ${fmtSize(sum)} / ${fmtSize(file.size)}`
    );
  };

  // 1パートをPUT（失効時はURL再発行、指数バックオフで最大4回再試行）
  async function putPart(idx) {
    const start = idx * part_size;
    const blob = file.slice(start, Math.min(start + part_size, file.size));
    let lastErr = null;
    for (let attempt = 0; attempt < 4; attempt++) {
      partLoaded[idx] = 0; refresh();
      try {
        etags[idx] = await xhrPut(partUrls[idx], blob, idx, partLoaded, refresh);
        partLoaded[idx] = blob.size; refresh();
        return;
      } catch (e) {
        if (e && e.unauth) throw e;
        lastErr = e;
        try {   // URLが失効していた可能性 → 該当パートだけ再発行
          const r = await fetch("/api/uploads/parts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key, upload_id, part_numbers: [idx + 1] }),
          });
          if (r.status === 401) { const u = new Error("unauth"); u.unauth = true; throw u; }
          if (r.ok) { const j = await r.json(); partUrls[idx] = j.part_urls[String(idx + 1)] || partUrls[idx]; }
        } catch (e2) { if (e2 && e2.unauth) throw e2; }
        await sleep(Math.min(1000 * 2 ** attempt, 8000));
      }
    }
    throw lastErr || new Error("パートの送信に失敗しました。");
  }

  // 並列3でパートを送信
  const CONCURRENCY = 3;
  let cursor = 0;
  async function worker() {
    while (cursor < part_count) { await putPart(cursor++); }
  }
  await Promise.all(
    Array.from({ length: Math.min(CONCURRENCY, part_count) }, worker)
  );

  // 確定（complete）
  const parts = etags.map((etag, i) => ({ PartNumber: i + 1, ETag: etag }));
  const compRes = await fetch("/api/uploads/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, upload_id, parts }),
  });
  if (compRes.status === 401) { const u = new Error("unauth"); u.unauth = true; throw u; }
  if (!compRes.ok) {
    const d = await compRes.json().catch(() => ({}));
    throw new Error(d.detail || "アップロードの確定に失敗しました。");
  }

  // 文字起こし開始（元ファイル名を引き回す）
  setProgress(99, "アップロード完了。文字起こしを開始します…");
  const body = new FormData();
  body.append("key", key);
  body.append("filename", file.name);
  body.append("language", $("language").value);
  const txRes = await fetch("/api/transcribe-key", { method: "POST", body });
  if (txRes.status === 401) { const u = new Error("unauth"); u.unauth = true; throw u; }
  if (!txRes.ok) {
    const d = await txRes.json().catch(() => ({}));
    throw new Error(d.detail || "文字起こしの開始に失敗しました。");
  }
  return (await txRes.json()).job_id;
}

// presigned URL へ PUT。進捗を partLoaded[idx] に反映し、ETag を返す。
function xhrPut(url, blob, idx, partLoaded, refresh) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) { partLoaded[idx] = e.loaded; refresh(); }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const etag = xhr.getResponseHeader("ETag");
        if (!etag) { reject(new Error("ETagを取得できませんでした（R2のCORSで ExposeHeaders: ETag を許可してください）。")); return; }
        resolve(etag);
      } else {
        reject(new Error("アップロード失敗 (HTTP " + xhr.status + ")"));
      }
    };
    xhr.onerror = () => reject(new Error("ネットワークエラー"));
    xhr.ontimeout = () => reject(new Error("タイムアウト"));
    xhr.send(blob);
  });
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
  updateGranDesc();
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

// 各切り方の説明（プルダウンの下に表示）。formats.py の GRAN_DESCRIPTIONS と対応。
const GRAN_DESC = {
  sentence: "文末（。！？）に加え、息継ぎ（無音）や長さでも区切る。いちばん細かい。",
  sec5: "約5秒ごとに時間（TimeCode）を表示。細かい頭出し向け。",
  sec10: "約10秒ごとに時間を表示。標準的なバランス。",
  sec30: "約30秒ごとに時間を表示。長い会議・講演向け。",
  min1: "約1分ごとに時間を表示。とても長い録音向け。",
  para_breath: "息継ぎ（無音の間）だけで段落分け。話し言葉の自然な区切り。",
  para_meaning: "無音＋文末＋長さから文意の切れ目を推測して段落分け。記事・議事録向け。",
  plain: "時間表示なしの読みやすい本文（段落分け）。清書・配布向け。",
};
function updateGranDesc() {
  const el = $("granDesc");
  if (el) el.textContent = GRAN_DESC[currentGran] || "";
}

$("gran").addEventListener("change", () => {
  currentGran = $("gran").value;
  updateGranDesc();
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
