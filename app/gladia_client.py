"""Gladia（ホスト型ASR）の話者分離つき文字起こし。

Whisper には話者分離が無いので、「誰がいつ話したか」が要るときだけこちらへ送る。
既定は Groq のまま（安い・速い）で、この経路はトグルON時だけ通る。

流れ（Async / 録音済みファイル）:
  1. POST /v2/upload         … 音声を渡して audio_url をもらう
  2. POST /v2/pre-recorded   … 文字起こしを依頼して id をもらう
  3. GET  /v2/pre-recorded/{id} … status が done になるまで問い合わせる

通信は groq_client.py と同じ「同期httpx」を使う。WindowsのasyncioのSSL実装は
アップロード時にまれに bad_record_mac を起こすため、呼び出し側で
asyncio.to_thread に載せる前提。
"""
from __future__ import annotations

import os
import time

import httpx

from . import config


class GladiaError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _headers() -> dict:
    if not config.GLADIA_API_KEY:
        raise GladiaError(0, "GLADIA_API_KEY が設定されていません。.env または環境変数に設定してください。")
    return {"x-gladia-key": config.GLADIA_API_KEY}


def _fail(resp: httpx.Response, what: str) -> GladiaError:
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or "")
    except Exception:
        pass
    detail = detail or (resp.text or "")[:300]
    return GladiaError(resp.status_code, f"{what}に失敗しました (HTTP {resp.status_code}) {detail}".strip())


def upload(path: str) -> str:
    """音声ファイルを渡して audio_url を得る。"""
    url = f"{config.GLADIA_BASE_URL}/upload"
    with open(path, "rb") as fh:
        files = {"audio": (os.path.basename(path), fh, "audio/mpeg")}
        with httpx.Client(timeout=httpx.Timeout(600.0), http2=False) as client:
            resp = client.post(url, headers=_headers(), files=files)
    if resp.status_code not in (200, 201):
        raise _fail(resp, "音声のアップロード")
    data = resp.json()
    audio_url = data.get("audio_url") or (data.get("result") or {}).get("audio_url")
    if not audio_url:
        raise GladiaError(resp.status_code, "アップロード応答に audio_url が含まれていません。")
    return audio_url


def request_transcription(audio_url: str, language: str | None, vocab: list[str] | None,
                          speakers: int = 0) -> str:
    """文字起こしを依頼して id を返す。

    speakers>0 のとき人数のヒントを渡す。ただし2026-07-23の実測ではこのヒントは
    効かなかった（指定しても5ラベルに割れた）ので、人数の保証は
    merge_minor_speakers（受け取った後の統合）側で行う。
    """
    payload: dict = {"audio_url": audio_url, "diarization": True}
    if speakers > 0:
        payload["diarization_config"] = {"number_of_speakers": speakers}
    if language:
        payload["language_config"] = {"languages": [language]}
    if vocab:
        # 固有名詞の表記補正。Whisper側は prompt 注入だが Gladia は専用の口がある。
        payload["custom_vocabulary"] = True
        payload["custom_vocabulary_config"] = {"vocabulary": vocab}

    with httpx.Client(timeout=httpx.Timeout(120.0), http2=False) as client:
        resp = client.post(
            f"{config.GLADIA_BASE_URL}/pre-recorded", headers=_headers(), json=payload
        )
    if resp.status_code not in (200, 201):
        raise _fail(resp, "文字起こしの依頼")
    job_id = resp.json().get("id")
    if not job_id:
        raise GladiaError(resp.status_code, "依頼の応答に id が含まれていません。")
    return job_id


def poll_result(job_id: str, timeout_seconds: float = 1800.0,
                on_wait=None) -> dict:
    """status が done になるまで問い合わせ、結果(result)を返す。

    on_wait(経過秒) が渡されていれば、待っている間の進捗表示に使う。
    """
    url = f"{config.GLADIA_BASE_URL}/pre-recorded/{job_id}"
    started = time.time()
    interval = 2.0
    with httpx.Client(timeout=httpx.Timeout(60.0), http2=False) as client:
        while True:
            resp = client.get(url, headers=_headers())
            if resp.status_code != 200:
                raise _fail(resp, "結果の取得")
            body = resp.json()
            status = body.get("status")
            if status == "done":
                return body.get("result") or {}
            if status == "error":
                err = body.get("error") or {}
                raise GladiaError(0, f"Gladia側で処理に失敗しました: {err}")

            elapsed = time.time() - started
            if elapsed > timeout_seconds:
                raise GladiaError(0, "Gladiaの処理が時間内に終わりませんでした。")
            if on_wait:
                on_wait(elapsed)
            time.sleep(interval)
            interval = min(interval * 1.3, 15.0)   # だんだん間隔を空ける


def transcribe_file(path: str, language: str | None = None,
                    vocab: list[str] | None = None, on_wait=None,
                    speakers: int = 0) -> dict:
    """1ファイルを話者分離つきで文字起こしする（upload→依頼→待ち を一括）。"""
    audio_url = upload(path)
    job_id = request_transcription(audio_url, language, vocab, speakers)
    return poll_result(job_id, on_wait=on_wait)


# ---------------------------------------------------------------- 応答の整形
def to_segments(result: dict) -> list[dict]:
    """Gladiaの utterances を、このアプリ共通の segments 形へ直す。

    共通形: [{start, end, text, speaker}]（speaker は 0 始まりの整数）
    """
    tr = result.get("transcription") or {}
    out: list[dict] = []
    for u in tr.get("utterances") or []:
        text = (u.get("text") or "").strip()
        if not text:
            continue
        speaker = u.get("speaker")
        out.append({
            "start": float(u.get("start") or 0.0),
            "end": float(u.get("end") or 0.0),
            "text": text,
            "speaker": int(speaker) if isinstance(speaker, (int, float)) else 0,
        })
    out.sort(key=lambda s: s["start"])
    return out


def billing_seconds(result: dict) -> float:
    """実際に課金された秒数（無料枠の実績確定に使う）。取れなければ 0.0。"""
    meta = result.get("metadata") or {}
    for key in ("billing_time", "audio_duration"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 0.0


def merge_minor_speakers(segments: list[dict], keep: int) -> list[dict]:
    """話者の過検出をならす。

    Gladiaは実質2人の対談でも5ラベル程度に割ることがある（2026-07-23 実測）。
    発話の長さ上位 keep 人だけを残し、それ以外のラベルは
    **時間的にいちばん近い採用話者**へ寄せる（前後の発話に吸収させる）。

    **keep=0（＝画面の「おまかせ」）のときは何もしない**＝Gladiaの判定をそのまま出す。
    実測データが2026-07-23の1件しか無い状態で「何秒未満は雑音」といった閾値を
    決めても推測にしかならないため、自動のならしは持たせていない。
    keep 以下しか話者がいない場合も何もしない。
    """
    if keep <= 0 or not segments:
        return segments

    totals: dict[int, float] = {}
    for s in segments:
        totals[s["speaker"]] = totals.get(s["speaker"], 0.0) + max(0.0, s["end"] - s["start"])
    if len(totals) <= keep:
        return segments

    major = {sp for sp, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:keep]}

    # 各セグメントについて、前後をたどって最も近い「採用話者」のラベルへ置き換える
    out = [dict(s) for s in segments]
    for i, s in enumerate(out):
        if s["speaker"] in major:
            continue
        prev_i = next((j for j in range(i - 1, -1, -1) if segments[j]["speaker"] in major), None)
        next_i = next((j for j in range(i + 1, len(segments)) if segments[j]["speaker"] in major), None)
        if prev_i is None and next_i is None:
            continue
        if prev_i is None:
            s["speaker"] = segments[next_i]["speaker"]
        elif next_i is None:
            s["speaker"] = segments[prev_i]["speaker"]
        else:
            gap_prev = s["start"] - segments[prev_i]["end"]
            gap_next = segments[next_i]["start"] - s["end"]
            s["speaker"] = segments[prev_i if gap_prev <= gap_next else next_i]["speaker"]

    # 残った話者番号を 0,1,2… に振り直す（画面の「話者1/話者2」を安定させる）
    order = sorted({s["speaker"] for s in out},
                   key=lambda sp: min(x["start"] for x in out if x["speaker"] == sp))
    renumber = {sp: i for i, sp in enumerate(order)}
    for s in out:
        s["speaker"] = renumber[s["speaker"]]
    return out
