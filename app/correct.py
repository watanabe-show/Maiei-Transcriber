"""校正LLM「文章を整える」。固有名詞・専門用語の**表記ゆれ／変換ミスだけ**を直す。

設計の要点（無料運用・安全性のため）:
  - Whisper とは別枠のチャットモデル（llama-3.3-70b-versatile）を使う。Whisperの
    1日2,000回の枠は消費しない。
  - セグメント本文を「番号: 本文」の行リストにして渡し、**同じ番号・同じ行数**で
    返させる。番号で突き合わせて本文だけ差し替えるので、タイムスタンプ（start/end）は保持。
  - LLMが行数・番号を崩した／余計な操作をしたバッチは、**まるごと原文を採用**して
    安全側に倒す（内容改変を絶対に混入させない）。
  - 長い本文は TPM（無料枠で概算12K/分）に収まるようバッチ分割。429は待って再試行。
  - 校正LLMの呼び出し回数（≒RPD）が増えすぎないよう、1ジョブのバッチ数に上限を設け、
    超える分は校正せず原文のまま残して警告する。
  - 語彙パックの語、および前バッチで実際に出現した語を「正しい表記」として毎回渡し、
    表記の一貫性を促す（追加のLLM呼び出しはしない＝TPMを圧迫しない）。
"""
from __future__ import annotations

import re
import time
from typing import Callable

from . import config, groq_client, vocab

# 1バッチに詰めるセグメント本文の合計文字数の目安（TPMに収める）。
BATCH_CHARS = 2000
# 1回の校正ジョブで許す最大バッチ数（≒LLM呼び出し回数。RPDの安全網）。
MAX_BATCHES = 80
# 429（レート制限）時の最大待機回数。
MAX_RATE_RETRIES = 6

_SYSTEM_PROMPT = (
    "あなたは日本語・英語の文字起こしの校正者です。"
    "許可された操作は『明らかな変換ミス・誤変換・固有名詞や専門用語の表記ゆれの修正』だけです。"
    "発言内容の言い換え・要約・意訳・追加・削除は一切禁止です。"
    "意味が不明瞭でも、原文の語順・内容・文体をそのまま保ちます。"
    "句読点の有無や口語表現も勝手に変えないでください（明らかな誤変換の修正に必要な範囲だけ整えます）。"
    "入力は『番号: 本文』の行の集まりです。各行の番号と本文の対応を必ず保ち、"
    "行の追加・削除・併合・分割・順序変更をしてはいけません。"
    "出力は入力と同じ行数・同じ番号で、『番号: 本文』の形式のみ。"
    "前置き・説明・コードブロックなど本文以外は一切書かないでください。"
)

_LINE_RE = re.compile(r"^\s*(\d+)\s*[:：]\s?(.*)$")


def available() -> bool:
    """校正が使えるか（APIキーがあるか）。"""
    return bool(config.GROQ_API_KEY)


# ---------------------------------------------------------------- バッチ分割
def _batch_segments(segments: list[dict], batch_chars: int | None = None) -> list[list[int]]:
    """セグメントのインデックスを、合計文字数がbatch_chars程度のバッチに分ける。

    返り値: [[seg_index, ...], ...]（空文字セグメントは含めない）
    """
    if batch_chars is None:
        batch_chars = BATCH_CHARS
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_len = 0
    for i, seg in enumerate(segments):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if cur and cur_len + len(text) > batch_chars:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(i)
        cur_len += len(text)
    if cur:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------- プロンプト
def _build_user_message(
    segments: list[dict], indices: list[int], terms: list[str], confirmed: list[str]
) -> str:
    lines = []
    if terms:
        lines.append("正しい表記とみなす語彙: " + "、".join(terms[:60]))
    if confirmed:
        lines.append("前の部分で確定した表記: " + "、".join(confirmed[:40]))
    lines.append("以下を校正してください（番号と行数を保ち、本文だけ必要な箇所を直す）:")
    for n, i in enumerate(indices, start=1):
        text = (segments[i].get("text") or "").strip().replace("\n", " ")
        lines.append(f"{n}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 応答パース
def _parse_reply(reply: str, expected: int) -> dict[int, str] | None:
    """『番号: 本文』の応答を {行番号(1始まり): 本文} に変換する。

    期待行数(expected)ぶんの番号がすべて揃わなければ None（=このバッチは原文採用）。
    """
    pairs: list[tuple[int, str]] = []
    for line in reply.splitlines():
        m = _LINE_RE.match(line)
        if m:
            pairs.append((int(m.group(1)), m.group(2).strip()))
    # 番号付き行がちょうど expected 個で、番号が 1..expected を過不足なく満たし、
    # かつ本文が空でないときだけ採用。少しでも崩れていれば None（=原文採用）。
    if len(pairs) != expected:
        return None
    out: dict[int, str] = {}
    for num, text in pairs:
        if not text or num in out:
            return None
        out[num] = text
    if sorted(out.keys()) != list(range(1, expected + 1)):
        return None
    return out


# ---------------------------------------------------------------- 1バッチ校正
def _correct_batch(user_msg: str, expected: int) -> dict[int, str] | None:
    """1バッチをLLMで校正。429は待って再試行。失敗時は None。"""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    rate_retries = 0
    while True:
        try:
            reply = groq_client.chat_complete(messages, temperature=0.0)
            return _parse_reply(reply, expected)
        except groq_client.GroqError as err:
            if err.status_code == 429 and rate_retries < MAX_RATE_RETRIES:
                rate_retries += 1
                wait = err.retry_after if err.retry_after else 5.0
                time.sleep(min(max(wait, 1.0), 30.0) + 1.0)
                continue
            # 429以外（キー誤り等）や上限到達 → このバッチは諦めて原文採用
            return None
        except Exception:
            return None


# ---------------------------------------------------------------- 出現語の抽出
def _appeared_terms(text: str, terms: list[str]) -> list[str]:
    """terms のうち text に実際に出現したものを返す（単純な部分一致。LLM不使用）。"""
    return [t for t in terms if t and t in text]


# ---------------------------------------------------------------- 本体
def polish_segments(
    segments: list[dict],
    language: str | None,
    pack_id: str | None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], str | None]:
    """segments の本文を校正した新しい segments を返す。

    返り値: (new_segments, note)。note は警告（上限超過など）または None。
    タイムスタンプ(start/end)は保持し、text だけ差し替える。校正できなかった
    バッチは原文のまま残すので、内容が失われることはない。
    """
    if not available():
        return segments, "校正は利用できません（GROQ_API_KEY未設定）。"

    terms = vocab.pack_terms(language, pack_id)
    batches = _batch_segments(segments)
    total = len(batches)
    note: str | None = None
    if total > MAX_BATCHES:
        note = (
            f"本文が長いため、前半{MAX_BATCHES}ブロックのみ校正しました"
            f"（無料枠保護のため）。残りは原文のままです。"
        )
        batches = batches[:MAX_BATCHES]

    # 原文をコピーして、校正できた行だけ差し替える
    new_segments = [dict(s) for s in segments]
    confirmed: list[str] = []  # これまでに確定した表記（一貫性のため次バッチへ渡す）

    done = 0
    for indices in batches:
        user_msg = _build_user_message(segments, indices, terms, confirmed)
        corrected = _correct_batch(user_msg, expected=len(indices))
        if corrected:
            for n, i in enumerate(indices, start=1):
                new_text = corrected.get(n)
                if new_text:
                    new_segments[i]["text"] = new_text
                    # 語彙のうち出現した語を確定表記として蓄積
                    for t in _appeared_terms(new_text, terms):
                        if t not in confirmed:
                            confirmed.append(t)
        done += 1
        if progress_cb:
            progress_cb(done, total)

    return new_segments, note
