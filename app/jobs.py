"""非同期ジョブ管理。

アップロードされたファイルごとに1ジョブを作り、バックグラウンドで
「音声変換 → 分割 → チャンクごとに文字起こし → タイムスタンプ連結」を行う。
フロントエンドは job_id を使って進捗をポーリングする。
"""
from __future__ import annotations

import asyncio
import shutil
import ssl
import time
import uuid

import httpx

from . import config, formats, groq_client, media

# 一時的な通信エラー（SSL bad record mac / 接続断 / タイムアウト等）。
# これらは自動でリトライする（ウイルス対策のHTTPS検査や不安定回線で起きやすい）。
_NETWORK_ERRORS = (httpx.HTTPError, ssl.SSLError, OSError)

# job_id -> job(dict)。単一インスタンス運用前提のインメモリ保存。
JOBS: dict[str, dict] = {}
# 完了/失敗から一定時間経過したジョブを掃除する（メモリ肥大防止）
_JOB_TTL_SECONDS = 60 * 60  # 1時間

# 同時に走る重い処理（ffmpeg+API）を制限して、無料インスタンスを守る
_SEMAPHORE = asyncio.Semaphore(2)


def create_job(
    filename: str, workdir: str, input_path: str, language: str, key: str | None = None
) -> str:
    """ジョブを作る。

    input_path はローカルファイルパス、または R2 の presigned GET URL（ffmpegは両方扱える）。
    key は R2 のオブジェクトキー。指定時は処理後に R2 から削除する。
    """
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "id": job_id,
        "filename": filename,
        "status": "queued",          # queued | processing | done | error
        "stage": "順番待ち…",
        "progress": 0,
        "error": None,
        "segments": None,
        "views": None,
        "text": None,
        "language": language,
        "_workdir": workdir,
        "_input_path": input_path,
        "_key": key,                 # R2経由のときだけ入る（後始末用）
        "created": time.time(),
        "updated": time.time(),
    }
    _cleanup_old()
    return job_id


def public_view(job: dict) -> dict:
    """フロントへ返す用に内部キー(_始まり)を除いたdictを作る。"""
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _set(job: dict, **kwargs) -> None:
    job.update(kwargs)
    job["updated"] = time.time()


def _cleanup_old() -> None:
    now = time.time()
    stale = [
        jid for jid, j in JOBS.items()
        if j["status"] in ("done", "error") and now - j["updated"] > _JOB_TTL_SECONDS
    ]
    for jid in stale:
        JOBS.pop(jid, None)


def start(job_id: str) -> None:
    """バックグラウンドタスクとして処理を開始する。"""
    task = asyncio.create_task(_run(job_id))
    # タスクがGCされないよう参照を保持
    JOBS[job_id]["_task"] = task


async def _run(job_id: str) -> None:
    job = JOBS.get(job_id)
    if job is None:
        return
    async with _SEMAPHORE:
        try:
            await _process(job)
        except Exception as exc:  # 想定外も必ずジョブに記録
            _set(job, status="error", stage="エラー", error=str(exc))
        finally:
            shutil.rmtree(job.get("_workdir", ""), ignore_errors=True)
            # R2経由ならアップロード済みオブジェクトを削除（成否によらず後始末）
            key = job.get("_key")
            if key:
                try:
                    from . import storage
                    storage.delete(key)
                except Exception:
                    pass


async def _process(job: dict) -> None:
    language = job["language"] or None

    # R2経由（大容量動画）は、ffmpegがURLから読み込むため変換に数分かかることがある。
    # 文言で待ち時間の理由を伝える。
    convert_stage = (
        "動画から音声を取り出しています（大きい動画は数分かかります）…"
        if job.get("_key")
        else "音声に変換中…"
    )
    _set(job, status="processing", stage=convert_stage, progress=5)
    seg_dir = job["_workdir"] + "/chunks"
    files = await asyncio.to_thread(
        media.transcode_and_segment, job["_input_path"], seg_dir
    )

    total = len(files)
    all_segments: list[dict] = []
    text_parts: list[str] = []
    offset = 0.0

    for i, fpath in enumerate(files):
        label = f"文字起こし中… ({i + 1}/{total})" if total > 1 else "文字起こし中…"
        _set(job, stage=label, progress=10 + int(85 * i / max(total, 1)))

        result = await _transcribe_with_retry(fpath, language, job)

        for seg in result.get("segments", []) or []:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            all_segments.append({
                "start": float(seg.get("start", 0.0)) + offset,
                "end": float(seg.get("end", 0.0)) + offset,
                "text": text,
            })
        part = (result.get("text") or "").strip()
        if part:
            text_parts.append(part)

        # 実際のチャンク長で次のオフセットを進める（タイムスタンプのズレを防ぐ）
        duration = float(result.get("duration") or 0.0)
        offset += duration if duration > 0 else float(config.CHUNK_SECONDS)

    # 全ての切り方（一文ごと/秒ごと/段落/TimeCodeなし）のブロックを一度に作る
    views = formats.build_views(all_segments)

    _set(
        job,
        status="done",
        stage="完了",
        progress=100,
        segments=all_segments,
        views=views,
        text=formats.to_readable_text(views["plain"]) if all_segments else "".join(text_parts).strip(),
    )


def _joined_text(segments: list[dict]) -> str:
    return "".join(seg["text"] for seg in segments).strip()


async def _transcribe_with_retry(fpath: str, language, job: dict) -> dict:
    """429（利用制限）とSSL/通信エラーを自動リトライしつつ文字起こしする。"""
    rate_wait = 5.0          # 429時の待機（指数的に増やす）
    rate_retries = 0
    net_retries = 0
    MAX_RATE_RETRIES = 6     # 利用制限の最大リトライ回数
    MAX_NET_RETRIES = 5      # 通信エラーの最大リトライ回数

    while True:
        try:
            # 同期httpxを別スレッドで実行（Windows asyncioのSSL不具合を回避）
            return await asyncio.to_thread(groq_client.transcribe_file, fpath, language)

        except groq_client.GroqError as err:
            # APIが429（レート制限）を返した場合 → 指定秒数だけ待って再試行
            if err.status_code == 429:
                rate_retries += 1
                if rate_retries > MAX_RATE_RETRIES:
                    raise RuntimeError(
                        "AIの利用制限が解除されませんでした。少し時間をおいて再実行してください。"
                    )
                wait = err.retry_after if err.retry_after else rate_wait
                wait = min(max(wait, 1.0), 90.0) + 1.0
                _set(job, stage=f"AI利用制限のため一時停止中… 約{int(wait)}秒")
                await asyncio.sleep(wait)
                rate_wait = min(rate_wait * 2, 60.0)
                continue
            # 401（キー誤り）/400 などは回復しないので即中断
            raise

        except _NETWORK_ERRORS as err:
            # SSL bad record mac / 接続断 / タイムアウト等 → 数回まで自動リトライ
            net_retries += 1
            if net_retries > MAX_NET_RETRIES:
                raise RuntimeError(
                    "通信エラーが続いたため中断しました（暗号化通信の不整合）。\n"
                    "原因として多いのは次のいずれかです：\n"
                    "・ウイルス対策ソフトのHTTPS検査機能（ESET / Kaspersky / Avast / AVG 等）\n"
                    "・VPN / 社内プロキシ / 不安定なWi-Fi\n"
                    "対処：上記HTTPS検査を一時停止、別回線(スマホのテザリング等)で試す、しばらく待って再実行。\n"
                    f"詳細: {type(err).__name__}: {err}"
                )
            wait = min(4.0 * net_retries, 20.0)
            _set(job, stage=f"通信エラーのため再試行中… {net_retries}回目（{int(wait)}秒後）")
            await asyncio.sleep(wait)
            continue
