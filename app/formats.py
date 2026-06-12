"""文字起こし結果（segments）の整形と各種テキスト出力。

segments: [{start, end, text}, ...]（Whisperのセグメント）
これを「無音の間（ま）」と「文末」で段落にまとめ、読みやすくする。
タイムスタンプ(TC)は段落の先頭にだけ付ける。
"""
from __future__ import annotations

_SENT_END = "。．.!?！？…」』）)"

# 画面・保存で選べる「切り方」。キー=内部値, 値=画面に出す日本語名。
# 並び順がそのままプルダウンの順序になる。
GRAN_LABELS = {
    "sentence": "一文ごと",
    "sec5": "5秒ごと",
    "sec10": "10秒ごと",
    "sec30": "30秒ごと",
    "min1": "1分ごと",
    "para_short": "段落ごと(短)",
    "para_long": "段落ごと(長)",
    "plain": "TimeCodeなし",
}
DEFAULT_GRAN = "sec10"


def _fmt_timestamp(seconds: float, comma: bool = True) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def hhmmss(seconds: float) -> str:
    """HH:MM:SS 形式（ミリ秒なし）。"""
    return _fmt_timestamp(seconds, comma=False)[:-4]


def group_paragraphs(
    segments: list[dict], gap: float = 0.9, max_chars: int = 120
) -> list[dict]:
    """セグメントを段落にまとめる。

    新しい段落に切り替える条件:
      ・直前との無音の間が gap 秒以上（話者交代・話題転換の目安）
      ・段落が max_chars 文字以上で、かつ文末記号で終わっている
    返り値: [{start, end, text}, ...]
    """
    paras: list[dict] = []
    cur: dict | None = None

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))

        if cur is None:
            cur = {"start": start, "end": end, "text": text}
            continue

        silence = start - cur["end"]
        long_enough = len(cur["text"]) >= max_chars and cur["text"][-1] in _SENT_END

        if silence >= gap or long_enough:
            paras.append(cur)
            cur = {"start": start, "end": end, "text": text}
        else:
            cur["text"] += text
            cur["end"] = end

    if cur:
        paras.append(cur)
    return paras


def group_time_blocks(segments: list[dict], interval: float = 10.0) -> list[dict]:
    """約 interval 秒ごとにTCを入れるためのブロックにまとめる。

    Whisperのセグメント境界（＝句読点・文末の位置）でのみ区切るので、
    TCは文の途中ではなく自然な切れ目に入る。
    返り値: [{start, end, text}, ...]（各ブロックの先頭時刻でTCを表示する）
    """
    blocks: list[dict] = []
    cur: dict | None = None

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))

        if cur is None:
            cur = {"start": start, "end": end, "text": text}
            continue

        # このブロックの先頭から interval 秒以上たっていれば、ここで区切る
        if (start - cur["start"]) >= interval:
            blocks.append(cur)
            cur = {"start": start, "end": end, "text": text}
        else:
            cur["text"] += text
            cur["end"] = end

    if cur:
        blocks.append(cur)
    return blocks


def group_sentences(segments: list[dict]) -> list[dict]:
    """一文ごとにまとめる。文末記号（。！？等）で終わったところで区切る。

    Whisperのセグメントは文の途中で切れることがあるので、文末記号が
    出るまで連結し、出たら1ブロックとして確定する。いちばん細かい切り方。
    返り値: [{start, end, text}, ...]
    """
    blocks: list[dict] = []
    cur: dict | None = None

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))

        if cur is None:
            cur = {"start": start, "end": end, "text": text}
        else:
            cur["text"] += text
            cur["end"] = end

        if cur["text"][-1] in _SENT_END:
            blocks.append(cur)
            cur = None

    if cur:
        blocks.append(cur)
    return blocks


def build_blocks(segments: list[dict], gran: str = DEFAULT_GRAN) -> list[dict]:
    """「切り方(gran)」に応じてブロック（先頭にTimeCodeを付ける単位）を作る。

    "plain"（TimeCodeなし）は段落区切りを返す（表示側で時間を出さない）。
    """
    if gran == "sentence":
        return group_sentences(segments)
    if gran == "sec5":
        return group_time_blocks(segments, 5)
    if gran == "sec30":
        return group_time_blocks(segments, 30)
    if gran == "min1":
        return group_time_blocks(segments, 60)
    if gran == "para_short":
        return group_paragraphs(segments, gap=0.6, max_chars=70)
    if gran == "para_long":
        return group_paragraphs(segments, gap=1.2, max_chars=250)
    if gran == "plain":
        return group_paragraphs(segments)
    return group_time_blocks(segments, 10)  # "sec10"（既定）


def build_views(segments: list[dict]) -> dict[str, list[dict]]:
    """全ての切り方のブロックを一度に作る（画面の切替表示用）。"""
    return {gran: build_blocks(segments, gran) for gran in GRAN_LABELS}


# ---------------------------------------------------------------- text outputs
def to_readable_text(paragraphs: list[dict]) -> str:
    """段落区切り・TCなしの読みやすい本文。"""
    return "\n\n".join(p["text"].strip() for p in paragraphs if p["text"].strip())


def to_readable_text_tc(blocks: list[dict]) -> str:
    """各ブロックの先頭に [HH:MM:SS] を付けた本文（約10秒ごと）。"""
    lines = []
    for b in blocks:
        text = b["text"].strip()
        if not text:
            continue
        lines.append(f"[{hhmmss(b['start'])}] {text}")
    return "\n".join(lines)


def to_srt(segments: list[dict]) -> str:
    blocks = []
    idx = 1
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = _fmt_timestamp(seg["start"], comma=True)
        end = _fmt_timestamp(seg["end"], comma=True)
        blocks.append(f"{idx}\n{start} --> {end}\n{text}\n")
        idx += 1
    return "\n".join(blocks)


def to_vtt(segments: list[dict]) -> str:
    out = ["WEBVTT", ""]
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = _fmt_timestamp(seg["start"], comma=False)
        end = _fmt_timestamp(seg["end"], comma=False)
        out.append(f"{start} --> {end}")
        out.append(text)
        out.append("")
    return "\n".join(out)


def render_text(segments: list[dict], fmt: str, gran: str = "") -> tuple[str, str, str]:
    """テキスト系フォーマットを (本文, 拡張子, MIME) で返す。

    fmt="txt" のとき gran（切り方）に従う。gran="plain" は時間なしの本文、
    それ以外は各ブロック先頭に [HH:MM:SS] を付けた本文。
    """
    fmt = (fmt or "txt").lower()
    if fmt == "srt":
        return to_srt(segments), "srt", "text/plain; charset=utf-8"
    if fmt == "vtt":
        return to_vtt(segments), "vtt", "text/vtt; charset=utf-8"

    if fmt == "txt_ts":  # 後方互換: 約10秒ごとにTimeCode
        return to_readable_text_tc(group_time_blocks(segments)), "txt", "text/plain; charset=utf-8"

    # fmt == "txt": 切り方に従う
    gran = gran or "plain"
    if gran == "plain":
        return to_readable_text(group_paragraphs(segments)), "txt", "text/plain; charset=utf-8"
    return to_readable_text_tc(build_blocks(segments, gran)), "txt", "text/plain; charset=utf-8"
