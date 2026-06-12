"""業種別の語彙パック（固有名詞リスト）の読み込みと、Whisper prompt の組み立て。

vocab_packs.json をリポジトリ直下から読み込む。選んだパックの固有名詞を
Whisper の `prompt` に注入すると、人名・専門用語の表記精度が上がる。

Whisper の prompt はモデル仕様上 **224トークン**（日本語で概算200字 / 英語で概算
800字）が上限で、超過分は黙って切り捨てられる。そこで本モジュールは生成 prompt が
上限に収まるよう、語彙を先頭から順に詰めて打ち切る「トークン上限ガード」を持つ。

このモジュールは語彙の中身に依存しないため、vocab_packs.json が無い／日本語パックが
空でも安全に動作する（その言語ではパック無し＝従来どおりの prompt になる）。
"""
from __future__ import annotations

import json
import os

# Whisper prompt のおおよその文字数上限（224トークン相当の安全側の目安）。
# 日本語は1文字あたりのトークン数が多いので小さめ、英語は大きめ。
CHAR_BUDGET = {"ja": 200, "en": 800}

# 語彙 prompt の接頭辞と、term の連結区切り（言語別）。
_PREFIX = {
    "ja": "報道番組のインタビュー音声です。固有名詞の表記例：",
    "en": "This is a news program interview. Names and terms: ",
}
_SEP = {"ja": "、", "en": ", "}

# 前チャンクから引き継ぐ末尾テキストの最大文字数（表記の連続性のためだけに使う）。
CARRYOVER_CHARS = 120

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_JSON_PATH = os.path.join(os.path.dirname(_BASE_DIR), "vocab_packs.json")

# {"ja": {pack_id: {"label": str, "terms": [str]}}, "en": {...}}
_PACKS: dict[str, dict] = {"ja": {}, "en": {}}


def _load() -> None:
    """vocab_packs.json を読み込む。無い／壊れていても落ちない。"""
    global _PACKS
    try:
        with open(_JSON_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        packs = data.get("packs") or {}
        _PACKS = {"ja": packs.get("ja") or {}, "en": packs.get("en") or {}}
    except FileNotFoundError:
        _PACKS = {"ja": {}, "en": {}}
    except Exception as exc:  # JSON破損など
        print(f"[警告] vocab_packs.json を読み込めませんでした（語彙パックは無効）: {exc}")
        _PACKS = {"ja": {}, "en": {}}


_load()


# ---------------------------------------------------------------- 参照
def list_packs() -> dict[str, list[dict]]:
    """UI用。言語ごとの [{id, label}] 一覧を返す。"""
    out: dict[str, list[dict]] = {}
    for lang in ("ja", "en"):
        out[lang] = [
            {"id": pid, "label": (p.get("label") or pid)}
            for pid, p in _PACKS.get(lang, {}).items()
        ]
    return out


def pack_terms(lang: str | None, pack_id: str | None) -> list[str]:
    """指定パックの term 配列を返す（無ければ空）。校正LLM・引き継ぎ抽出でも使う。"""
    if not lang or not pack_id:
        return []
    pack = _PACKS.get(lang, {}).get(pack_id)
    if not pack:
        return []
    return [t for t in (pack.get("terms") or []) if t]


# ---------------------------------------------------------------- prompt 組み立て
def _fit_terms(prefix: str, sep: str, terms: list[str], limit: int) -> tuple[str, bool]:
    """prefix + sep連結のterms を limit 文字以内に収める。

    terms を先頭から順に詰め、次を足すと超える時点で打ち切る。
    返り値: (生成文字列, 打ち切りが起きたか)
    """
    chosen: list[str] = []
    length = len(prefix)
    truncated = False
    for term in terms:
        add = len(term) + (len(sep) if chosen else 0)
        if length + add > limit:
            truncated = True
            break
        chosen.append(term)
        length += add
    return prefix + sep.join(chosen), truncated


def build_vocab_prompt(lang: str | None, pack_id: str | None) -> str | None:
    """語彙パック由来の Whisper prompt を作る（トークン上限ガード込み）。

    パック未選択・対象外言語・語彙空なら None。
    """
    if lang not in ("ja", "en"):
        return None
    terms = pack_terms(lang, pack_id)
    if not terms:
        return None
    text, truncated = _fit_terms(_PREFIX[lang], _SEP[lang], terms, CHAR_BUDGET[lang])
    if truncated:
        print(
            f"[警告] 語彙パック '{pack_id}' が Whisper prompt の上限"
            f"（{CHAR_BUDGET[lang]}字）を超えたため一部の語を省略しました。"
        )
    return text


def tail_for_carryover(text: str | None, lang: str | None) -> str:
    """前チャンクの末尾を、次チャンクへ引き継ぐ短い文字列にする（ja/enのみ）。"""
    if not text or lang not in ("ja", "en"):
        return ""
    return text.strip()[-CARRYOVER_CHARS:]


def compose_prompt(lang: str | None, base: str | None, tail: str | None) -> str | None:
    """Whisper へ渡す最終 prompt を組み立てる。

    base : 語彙パック由来の prompt（無ければ句読点プライミング等。Noneも可）。
    tail : 前チャンクの末尾（表記連続性のため）。base の後ろに、上限の余白がある時だけ足す。
    最後に必ずトークン上限ガード（文字数）でクランプする。語彙パックが優先。
    """
    if lang not in ("ja", "en"):
        # 自動判定など：言語プライミングはしない（従来どおり base をそのまま）。
        return base
    limit = CHAR_BUDGET[lang]
    base = base or ""
    if tail:
        # base の後ろに余白がある分だけ tail を足す（区切りの空白1文字を見込む）。
        room = limit - len(base) - 1
        if room >= 20:  # ある程度の余白がある時だけ引き継ぐ
            sep = " " if base else ""
            text = base + sep + tail[-room:]
        else:
            text = base
    else:
        text = base
    text = text[:limit]
    return text or None
