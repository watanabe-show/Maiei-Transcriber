"""FastAPI 本体。フロント配信・ログイン・アップロード・進捗・ダウンロードを束ねる。"""
from __future__ import annotations

import math
import os
import tempfile

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config, documents, formats, jobs, storage, vocab

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
STATIC_DIR = os.path.join(PROJECT_DIR, "static")
IMAGES_DIR = os.path.join(PROJECT_DIR, "images")

app = FastAPI(title="音声文字起こし", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    same_site="lax",
    https_only=False,  # ローカル(http)でも動くように。公開時はHTTPS推奨
    max_age=60 * 60 * 24 * 14,  # 2週間
)


@app.on_event("startup")
async def _startup() -> None:
    if config.AUTH_DISABLED_WARNING:
        pw = next(iter(config.PASSWORDS))
        print("=" * 64)
        print("[警告] APP_PASSWORD が未設定です。今回の自動生成パスワード:")
        print(f"        ログインパスワード = {pw}")
        print("        本番運用では .env / 環境変数に APP_PASSWORD を設定してください。")
        print("=" * 64)
    if not config.GROQ_API_KEY:
        print("[警告] GROQ_API_KEY が未設定です。文字起こしは実行できません（.env に設定してください）。")


# ---------------------------------------------------------------- helpers
def _is_authed(request: Request) -> bool:
    return bool(request.session.get("auth"))


def _require_auth(request: Request) -> None:
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="ログインが必要です。")


def _read_html(name: str) -> str:
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    page = "index.html" if _is_authed(request) else "login.html"
    return HTMLResponse(_read_html(page))


_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<text y="0.9em" font-size="90">🎙️</text></svg>'
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    # ブラウザが自動取得するタブ用アイコン（無いと404ログが出るので用意）
    return Response(content=_FAVICON, media_type="image/svg+xml")


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request) -> HTMLResponse:
    # 使い方は機密情報を含まないので、ログイン前（ログイン画面の導線）からも見られるようにする
    return HTMLResponse(_read_html("help.html"))


# ---------------------------------------------------------------- auth API
@app.get("/api/me")
async def me(request: Request) -> JSONResponse:
    return JSONResponse({"authed": _is_authed(request)})


@app.post("/api/login")
async def login(request: Request, password: str = Form(...)) -> JSONResponse:
    if password.strip() in config.PASSWORDS:
        request.session["auth"] = True
        return JSONResponse({"ok": True})
    raise HTTPException(status_code=401, detail="パスワードが違います。")


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- transcription API
@app.get("/api/vocab")
async def api_vocab(request: Request) -> JSONResponse:
    """語彙パックの一覧（言語別 [{id,label}]）を返す。フロントの選択肢に使う。"""
    _require_auth(request)
    return JSONResponse(vocab.list_packs())


@app.post("/api/transcribe")
async def transcribe(
    request: Request, file: UploadFile, language: str = Form(""), pack_id: str = Form("")
) -> JSONResponse:
    _require_auth(request)

    filename = file.filename or "audio"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in config.ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"対応していない形式です（{ext or '不明'}）。音声(mp3,m4a,wav,aac等)または動画(mp4,mov等)を選んでください。",
        )

    lang = "" if language.strip().lower() in ("", "auto") else language.strip().lower()

    workdir = tempfile.mkdtemp(prefix="tx_")
    input_path = os.path.join(workdir, "input" + ext)
    limit_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    total = 0

    try:
        with open(input_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"ファイルが大きすぎます（上限 {config.MAX_UPLOAD_MB}MB）。長い動画はローカル起動での利用をおすすめします。",
                    )
                out.write(chunk)
    except HTTPException:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
        raise
    finally:
        await file.close()

    if total == 0:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="空のファイルです。")

    job_id = jobs.create_job(filename, workdir, input_path, lang, pack_id=pack_id.strip() or None)
    jobs.start(job_id)
    return JSONResponse({"job_id": job_id})


@app.get("/api/jobs/{job_id}")
async def job_status(request: Request, job_id: str) -> JSONResponse:
    _require_auth(request)
    job = jobs.JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません（時間切れの可能性があります）。")
    return JSONResponse(jobs.public_view(job))


def _attachment(data: bytes, base: str, ext: str, mime: str) -> Response:
    """日本語ファイル名にも対応した添付ダウンロード応答。"""
    from urllib.parse import quote

    safe = quote(f"{base}.{ext}")
    headers = {
        "Content-Disposition": f"attachment; filename=transcript.{ext}; filename*=UTF-8''{safe}"
    }
    return Response(content=data, media_type=mime, headers=headers)


@app.get("/api/jobs/{job_id}/download")
async def job_download(
    request: Request,
    job_id: str,
    fmt: str = "txt",
    gran: str = formats.DEFAULT_GRAN,
) -> Response:
    _require_auth(request)
    job = jobs.JOBS.get(job_id)
    if job is None or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="完了したジョブが見つかりません。")

    segments = job.get("segments") or []
    base = os.path.splitext(job.get("filename") or "transcript")[0] or "transcript"
    fmt = (fmt or "txt").lower()
    gran = (gran or formats.DEFAULT_GRAN).lower()

    if fmt == "docx":
        blocks = formats.build_blocks(segments, gran)
        data = documents.build_docx(
            blocks, title=base, subtitle="文字起こし", show_tc=(gran != "plain")
        )
        return _attachment(data, base, "docx", documents.DOCX_MIME)
    if fmt == "xlsx":
        blocks = formats.build_blocks(segments, gran)
        label = formats.GRAN_LABELS.get(gran, "本文")
        body_label = "本文" if gran == "plain" else f"本文（{label}）"
        data = documents.build_xlsx(blocks, segments, body_label=body_label)
        return _attachment(data, base, "xlsx", documents.XLSX_MIME)

    body, ext, mime = formats.render_text(segments, fmt, gran)
    return _attachment(body.encode("utf-8"), base, ext, mime)


# ---------------------------------------------------------------- direct upload (R2) API
# 大容量動画むけ。ブラウザ → R2 へ multipart で直接アップロードし、サーバーは
# presigned GET URL を ffmpeg にストリーミング入力する。R2未設定なら storage_enabled=false
# となり、フロントは従来のサーバー直アップロードだけを使う（完全に任意の機能）。
@app.get("/api/config")
async def app_config(request: Request) -> JSONResponse:
    _require_auth(request)
    return JSONResponse({
        "storage_enabled": storage.enabled(),
        "direct_max_mb": config.DIRECT_UPLOAD_MAX_MB,
        "storage_max_mb": config.STORAGE_MAX_UPLOAD_MB,
        "part_mb": config.UPLOAD_PART_MB,
    })


def _require_storage() -> None:
    if not storage.enabled():
        raise HTTPException(status_code=400, detail="大容量アップロード（ストレージ）が未設定です。")


@app.post("/api/uploads/initiate")
async def uploads_initiate(request: Request) -> JSONResponse:
    _require_auth(request)
    _require_storage()
    data = await request.json()
    filename = (str(data.get("filename") or "audio")).strip() or "audio"
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    ext = os.path.splitext(filename)[1].lower()
    if ext not in config.ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"対応していない形式です（{ext or '不明'}）。",
        )
    if size <= 0:
        raise HTTPException(status_code=400, detail="ファイルサイズが不明です。")
    if size > config.STORAGE_MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"ファイルが大きすぎます（上限 {config.STORAGE_MAX_UPLOAD_MB}MB）。",
        )

    part_size = config.UPLOAD_PART_MB * 1024 * 1024
    part_count = max(1, math.ceil(size / part_size))
    key = storage.new_key(ext)
    try:
        upload_id = storage.initiate(key)
        urls = storage.presign_parts(
            key, upload_id, range(1, part_count + 1), config.PRESIGN_PUT_EXPIRE
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ストレージへの接続に失敗しました: {exc}")
    return JSONResponse({
        "key": key,
        "upload_id": upload_id,
        "part_size": part_size,
        "part_count": part_count,
        "part_urls": [urls[n] for n in range(1, part_count + 1)],
    })


@app.post("/api/uploads/parts")
async def uploads_parts(request: Request) -> JSONResponse:
    """失効・再開時に、指定パートの presigned PUT URL を再発行する。"""
    _require_auth(request)
    _require_storage()
    data = await request.json()
    key = data.get("key")
    upload_id = data.get("upload_id")
    nums = data.get("part_numbers") or []
    if not storage.is_valid_key(key) or not upload_id or not nums:
        raise HTTPException(status_code=400, detail="パラメータが不足しています。")
    urls = storage.presign_parts(key, upload_id, [int(n) for n in nums], config.PRESIGN_PUT_EXPIRE)
    return JSONResponse({"part_urls": {str(n): u for n, u in urls.items()}})


@app.post("/api/uploads/complete")
async def uploads_complete(request: Request) -> JSONResponse:
    _require_auth(request)
    _require_storage()
    data = await request.json()
    key = data.get("key")
    upload_id = data.get("upload_id")
    parts = data.get("parts") or []
    if not storage.is_valid_key(key) or not upload_id or not parts:
        raise HTTPException(status_code=400, detail="パラメータが不足しています。")
    try:
        storage.complete(key, upload_id, parts)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"アップロードの確定に失敗しました: {exc}")
    return JSONResponse({"ok": True})


@app.post("/api/uploads/abort")
async def uploads_abort(request: Request) -> JSONResponse:
    _require_auth(request)
    if not storage.enabled():
        return JSONResponse({"ok": True})
    data = await request.json()
    key = data.get("key")
    upload_id = data.get("upload_id")
    if storage.is_valid_key(key) and upload_id:
        storage.abort(key, upload_id)
    return JSONResponse({"ok": True})


@app.post("/api/transcribe-key")
async def transcribe_key(
    request: Request,
    key: str = Form(...),
    filename: str = Form("audio"),
    language: str = Form(""),
    pack_id: str = Form(""),
) -> JSONResponse:
    """R2にアップ済みのオブジェクト(key)を文字起こしする。

    既存の /api/transcribe（サーバー直アップ）は一切変更せず、R2経路はこの別口で受ける。
    filename は元のファイル名（ダウンロード名の元になるため引き回す）。
    """
    _require_auth(request)
    _require_storage()
    if not storage.is_valid_key(key):
        raise HTTPException(status_code=400, detail="不正なキーです。")
    filename = (filename or "audio").strip() or "audio"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in config.ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"対応していない形式です（{ext or '不明'}）。")
    lang = "" if language.strip().lower() in ("", "auto") else language.strip().lower()

    try:
        get_url = storage.presign_get(key, config.PRESIGN_GET_EXPIRE)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"読み取りURLの発行に失敗しました: {exc}")

    # チャンク用の作業ディレクトリだけ用意（入力本体はR2からストリーミングする）
    workdir = tempfile.mkdtemp(prefix="tx_")
    job_id = jobs.create_job(
        filename, workdir, get_url, lang, key=key, pack_id=pack_id.strip() or None
    )
    jobs.start(job_id)
    return JSONResponse({"job_id": job_id})


# 画像・静的ファイルのマウント（最後に置いてAPIルートを優先させる）
if os.path.isdir(IMAGES_DIR):
    app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
