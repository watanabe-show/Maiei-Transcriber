"""FastAPI 本体。フロント配信・ログイン・アップロード・進捗・ダウンロードを束ねる。"""
from __future__ import annotations

import os
import tempfile

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config, documents, formats, jobs

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
    page = "help.html" if _is_authed(request) else "login.html"
    return HTMLResponse(_read_html(page))


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
@app.post("/api/transcribe")
async def transcribe(request: Request, file: UploadFile, language: str = Form("")) -> JSONResponse:
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

    job_id = jobs.create_job(filename, workdir, input_path, lang)
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
    request: Request, job_id: str, fmt: str = "txt", gran: str = formats.DEFAULT_GRAN
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


# 画像・静的ファイルのマウント（最後に置いてAPIルートを優先させる）
if os.path.isdir(IMAGES_DIR):
    app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
