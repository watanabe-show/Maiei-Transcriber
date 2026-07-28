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
    "para_breath": "段落ごと(息継ぎ)",
    "para_meaning": "段落ごと(文意)",
    "plain": "TimeCodeなし",
}

# 各切り方の説明（画面のプルダウン下・使い方ページで共用）。
GRAN_DESCRIPTIONS = {
    "sentence": "文末（。！？）に加え、息継ぎ（無音）や長さでも区切る。いちばん細かい。",
    "sec5": "約5秒ごとに時間（TimeCode）を表示。細かい頭出し向け。",
    "sec10": "約10秒ごとに時間を表示。標準的なバランス。",
    "sec30": "約30秒ごとに時間を表示。長い会議・講演向け。",
    "min1": "約1分ごとに時間を表示。とても長い録音向け。",
    "para_breath": "息継ぎ（無音の間）だけで段落分け。話し言葉の自然な区切り。",
    "para_meaning": "無音＋文末＋長さから文意の切れ目を推測して段落分け。記事・議事録向け。",
    "plain": "時間表示なしの読みやすい本文（段落分け）。清書・配布向け。",
}

# 古い切り方キーの後方互換（保存URL等で渡ってきたとき用）。
_GRAN_ALIASES = {"para_short": "para_breath", "para_long": "para_meaning"}

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


def group_sentences(
    segments: list[dict], gap: float = 0.6, max_chars: int = 60
) -> list[dict]:
    """一文ごとにまとめる。

    文末記号（。！？等）で区切るのが基本だが、Whisperの日本語出力は
    句読点が乏しく、それだけだと巨大な1ブロックになりがち。そこで、
    句読点が無くても次のいずれかで区切る:
      ・直前との無音の間（息継ぎ）が gap 秒以上
      ・1ブロックが max_chars 文字以上になった（際限ない連結を防ぐ）
    これにより、句読点が少ない音声でも自然な一文単位に近づく。
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

        if cur is not None:
            ended = cur["text"][-1] in _SENT_END
            silence = start - cur["end"]
            too_long = len(cur["text"]) >= max_chars
            if ended or silence >= gap or too_long:
                blocks.append(cur)
                cur = None

        if cur is None:
            cur = {"start": start, "end": end, "text": text}
        else:
            cur["text"] += text
            cur["end"] = end

    if cur:
        blocks.append(cur)
    return blocks


def group_breath(segments: list[dict], gap: float = 0.8) -> list[dict]:
    """息継ぎ（無音の間）だけで段落にまとめる。

    文末記号や文字数は見ず、純粋に「間（ま）が gap 秒以上空いたら段落を切る」。
    話し言葉の自然な呼吸の区切りに沿うので、句読点に依存しない。
    返り値: [{start, end, text}, ...]（各段落の先頭時刻でTCを表示する）
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

        if (start - cur["end"]) >= gap:
            paras.append(cur)
            cur = {"start": start, "end": end, "text": text}
        else:
            cur["text"] += text
            cur["end"] = end

    if cur:
        paras.append(cur)
    return paras


def speaker_label(speaker) -> str:
    """0始まりの話者番号を「話者1」の形にする。"""
    return f"話者{int(speaker) + 1}"


def has_speakers(segments: list[dict]) -> bool:
    """話者分離つきの結果か（話者分離モードで作られた segments か）。"""
    return any(s.get("speaker") is not None for s in segments)


def _runs_by_speaker(segments: list[dict]) -> list[list[dict]]:
    """同じ話者が続く区間ごとに segments を分ける。"""
    runs: list[list[dict]] = []
    for seg in segments:
        if runs and runs[-1][0].get("speaker") == seg.get("speaker"):
            runs[-1].append(seg)
        else:
            runs.append([seg])
    return runs


def build_blocks(segments: list[dict], gran: str = DEFAULT_GRAN) -> list[dict]:
    """「切り方(gran)」に応じてブロック（先頭にTimeCodeを付ける単位）を作る。

    "plain"（TimeCodeなし）は段落区切りを返す（表示側で時間を出さない）。

    話者分離つきの結果では、**話者が変わったら必ず区切る**（別人の発言が
    1ブロックに混ざらないように）。切り方ごとのまとめ方（group_*）には手を入れず、
    先に話者の連続区間へ割ってから各区間に適用する。話者情報が無い場合は
    従来と完全に同じ経路を通る。
    """
    if has_speakers(segments):
        blocks: list[dict] = []
        for run in _runs_by_speaker(segments):
            for b in _build_blocks_one(run, gran):
                b["speaker"] = run[0].get("speaker")
                blocks.append(b)
        blocks.sort(key=lambda b: b["start"])
        return blocks
    return _build_blocks_one(segments, gran)


def _build_blocks_one(segments: list[dict], gran: str = DEFAULT_GRAN) -> list[dict]:
    gran = _GRAN_ALIASES.get(gran, gran)  # 旧キー(para_short/long)を新キーへ
    if gran == "sentence":
        return group_sentences(segments)
    if gran == "sec5":
        return group_time_blocks(segments, 5)
    if gran == "sec30":
        return group_time_blocks(segments, 30)
    if gran == "min1":
        return group_time_blocks(segments, 60)
    if gran == "para_breath":
        return group_breath(segments, gap=0.8)
    if gran == "para_meaning":
        return group_paragraphs(segments, gap=1.0, max_chars=200)
    if gran == "plain":
        return group_paragraphs(segments)
    return group_time_blocks(segments, 10)  # "sec10"（既定）


def build_views(segments: list[dict]) -> dict[str, list[dict]]:
    """全ての切り方のブロックを一度に作る（画面の切替表示用）。"""
    return {gran: build_blocks(segments, gran) for gran in GRAN_LABELS}


# ---------------------------------------------------------------- text outputs
def body_text(block: dict) -> str:
    """本文。話者分離つきなら「話者1：」を頭に付ける。"""
    text = (block.get("text") or "").strip()
    if not text:
        return ""
    if block.get("speaker") is None:
        return text
    return f"{speaker_label(block['speaker'])}：{text}"


def to_readable_text(paragraphs: list[dict]) -> str:
    """段落区切り・TCなしの読みやすい本文。"""
    return "\n\n".join(t for t in (body_text(p) for p in paragraphs) if t)


def to_readable_text_tc(blocks: list[dict]) -> str:
    """各ブロックの先頭に [HH:MM:SS] を付けた本文（約10秒ごと）。"""
    lines = []
    for b in blocks:
        text = body_text(b)
        if not text:
            continue
        lines.append(f"[{hhmmss(b['start'])}] {text}")
    return "\n".join(lines)


def to_srt(segments: list[dict]) -> str:
    blocks = []
    idx = 1
    for seg in segments:
        text = body_text(seg)
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
        text = body_text(seg)
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
    # "plain" も build_blocks を通す（話者分離つきのとき話者ごとに区切るため。
    #  話者情報が無い場合 build_blocks("plain") は group_paragraphs と同一）
    gran = gran or "plain"
    blocks = build_blocks(segments, gran)
    if gran == "plain":
        return to_readable_text(blocks), "txt", "text/plain; charset=utf-8"
    return to_readable_text_tc(blocks), "txt", "text/plain; charset=utf-8"
