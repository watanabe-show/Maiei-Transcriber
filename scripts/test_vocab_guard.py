"""語彙パックの Whisper prompt トークン上限ガードのテスト。

ネットワーク不要。`python scripts/test_vocab_guard.py` で実行する。
Whisper prompt は224トークン上限のため、語彙が多くても生成 prompt が
文字数上限（ja=200 / en=800）に必ず収まること、語彙パックが引き継ぎより
優先されること、自動判定では言語プライミングしないことを確認する。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import vocab  # noqa: E402


def test_fit_terms_truncates_within_limit():
    terms = ["あいうえお" * 4] * 50  # 1語20字 × 50語（明らかに上限超過）
    text, truncated = vocab._fit_terms("接頭：", "、", terms, 100)
    assert len(text) <= 100, len(text)
    assert truncated is True


def test_build_vocab_prompt_ja_within_budget():
    vocab._PACKS["ja"]["__test_big"] = {"label": "t", "terms": ["長い専門用語" * 4] * 40}
    p = vocab.build_vocab_prompt("ja", "__test_big")
    assert p is not None
    assert len(p) <= vocab.CHAR_BUDGET["ja"], len(p)
    assert p.startswith("報道番組")


def test_build_vocab_prompt_en_within_budget():
    vocab._PACKS["en"]["__test_big"] = {"label": "t", "terms": ["Some Long Term Name"] * 100}
    p = vocab.build_vocab_prompt("en", "__test_big")
    assert p is not None
    assert len(p) <= vocab.CHAR_BUDGET["en"], len(p)


def test_none_when_no_pack_or_auto():
    assert vocab.build_vocab_prompt("ja", "does_not_exist") is None
    assert vocab.build_vocab_prompt("ja", "") is None
    assert vocab.build_vocab_prompt("auto", "anything") is None
    assert vocab.build_vocab_prompt(None, "anything") is None


def test_compose_clamps_and_vocab_priority():
    # base が上限いっぱい → 引き継ぎ(tail)は省略され、上限も超えない
    base = "あ" * vocab.CHAR_BUDGET["ja"]
    out = vocab.compose_prompt("ja", base, "ぜったいに入れたい末尾テキスト")
    assert len(out) <= vocab.CHAR_BUDGET["ja"]
    assert "末尾テキスト" not in out


def test_compose_appends_tail_when_room():
    base = "報道番組のインタビュー音声です。"
    out = vocab.compose_prompt("ja", base, "前チャンクの末尾の文章です")
    assert out.startswith(base)
    assert "末尾の文章です" in out
    assert len(out) <= vocab.CHAR_BUDGET["ja"]


def test_compose_auto_passthrough():
    # 自動判定では言語プライミングしない（base をそのまま／Noneも維持）
    assert vocab.compose_prompt(None, "base", "tail") == "base"
    assert vocab.compose_prompt("auto", None, "tail") is None


def test_carryover_tail_length():
    long_text = "あ" * 500
    tail = vocab.tail_for_carryover(long_text, "ja")
    assert len(tail) == vocab.CARRYOVER_CHARS
    assert vocab.tail_for_carryover(long_text, "auto") == ""
    assert vocab.tail_for_carryover("", "ja") == ""


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
