"""Groq の Whisper 音声文字起こしAPI（OpenAI互換）への薄いラッパー。"""
from __future__ import annotations

import os

import httpx

from . import config


class GroqError(RuntimeError):
    def __init__(self, status_code: int, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after


def transcribe_file(path: str, language: str | None = None, prompt: str | None = None) -> dict:
    """1ファイル（=1チャンク）を文字起こしし、verbose_json をdictで返す。

    返るdictの主なキー: text(全文), segments(start/end/text の配列), duration(秒)。
    429 などのエラーは GroqError として送出する（retry_after を含む場合あり）。

    通信は「同期httpx」を使う。WindowsのasyncioのSSL実装はアップロード時に
    まれに bad_record_mac を起こすため、安定している同期通信を採用し、
    呼び出し側で asyncio.to_thread を使ってイベントループを塞がないようにする。
    """
    if not config.GROQ_API_KEY:
        raise GroqError(0, "GROQ_API_KEY が設定されていません。.env または環境変数に設定してください。")

    url = f"{config.GROQ_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    data = {
        "model": config.GROQ_MODEL,
        "response_format": "verbose_json",
        "temperature": "0",
    }
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt

    with open(path, "rb") as fh:
        files = {"file": (os.path.basename(path), fh, "audio/mpeg")}
        with httpx.Client(timeout=httpx.Timeout(600.0), http2=False) as client:
            resp = client.post(url, headers=headers, data=data, files=files)

    if resp.status_code == 200:
        return resp.json()

    # --- エラー処理 ---
    retry_after = _parse_retry_after(resp)
    detail = _extract_error_message(resp)
    raise GroqError(resp.status_code, detail, retry_after=retry_after)


def chat_complete(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: float = 120.0,
) -> str:
    """Groq のチャット補完（OpenAI互換 /chat/completions）。校正LLMに使う。

    messages: [{"role": "system"|"user"|"assistant", "content": str}, ...]
    返り値: アシスタントの本文テキスト。429 等は GroqError として送出する。
    """
    if not config.GROQ_API_KEY:
        raise GroqError(0, "GROQ_API_KEY が設定されていません。")

    url = f"{config.GROQ_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    data: dict = {
        "model": model or config.CORRECT_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        data["max_tokens"] = max_tokens

    with httpx.Client(timeout=httpx.Timeout(timeout), http2=False) as client:
        resp = client.post(url, headers=headers, json=data)

    if resp.status_code == 200:
        body = resp.json()
        return (body["choices"][0]["message"]["content"] or "").strip()

    retry_after = _parse_retry_after(resp)
    detail = _extract_error_message(resp)
    raise GroqError(resp.status_code, detail, retry_after=retry_after)


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """Retry-After ヘッダ、または本文の 'try again in X' から待ち秒数を推定。"""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    import re

    m = re.search(r"try again in ([\d.]+)s", resp.text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _extract_error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str):
                return err
    except Exception:
        pass
    return (resp.text or f"HTTP {resp.status_code}")[:500]
