"""校正LLM（app.correct）の安全性ロジックのテスト。ネットワーク不要（chat_completeをモック）。

最重要の確認: LLMが行数・番号を崩した／例外を投げたバッチは**原文のまま残り**、
内容が失われない（タイムスタンプも保持）こと。`python scripts/test_correct.py` で実行。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import correct  # noqa: E402

# available() を常に True に（APIキーの有無に依存させない）
correct.config.GROQ_API_KEY = "test-key"


def _segs():
    return [
        {"start": 0.0, "end": 1.0, "text": "こんにちは"},
        {"start": 1.0, "end": 2.0, "text": "せかい"},
        {"start": 2.0, "end": 3.0, "text": ""},        # 空はスキップされる
        {"start": 3.0, "end": 4.0, "text": "おわり"},
    ]


def test_parse_reply_ok():
    assert correct._parse_reply("1: あ\n2: い", 2) == {1: "あ", 2: "い"}
    # 全角コロン・前後ゴミ行は無視
    assert correct._parse_reply("前置き\n1： あ\n2： い\nおまけ", 2) == {1: "あ", 2: "い"}


def test_parse_reply_count_mismatch_returns_none():
    assert correct._parse_reply("1: あ", 2) is None          # 足りない
    assert correct._parse_reply("1: あ\n2: い\n3: う", 2) is None  # 多い


def test_batch_segments_skips_empty_and_splits():
    segs = _segs()
    correct.BATCH_CHARS_BACKUP = correct.BATCH_CHARS
    try:
        correct.BATCH_CHARS = 6   # 強制的に細かく分割
        batches = correct._batch_segments(segs)
        flat = [i for b in batches for i in b]
        assert 2 not in flat            # 空セグメントは含まれない
        assert flat == [0, 1, 3]
        assert len(batches) >= 2        # 分割されている
    finally:
        correct.BATCH_CHARS = correct.BATCH_CHARS_BACKUP


def test_appeared_terms():
    assert correct._appeared_terms("高市総理が会見", ["高市早苗", "高市", "林芳正"]) == ["高市"]


def _fake_good(messages, **kw):
    """番号を保ったまま『せかい→世界』だけ直す良い応答。"""
    user = messages[-1]["content"]
    out = []
    for line in user.splitlines():
        m = re.match(r"^(\d+):\s?(.*)$", line)
        if m:
            out.append(f"{m.group(1)}: {m.group(2).replace('せかい', '世界')}")
    return "\n".join(out)


def test_polish_applies_good_corrections_and_keeps_timestamps():
    correct.groq_client.chat_complete = _fake_good
    new, note = correct.polish_segments(_segs(), "ja", None)
    assert note is None
    assert new[1]["text"] == "世界"          # 修正された
    assert new[0]["text"] == "こんにちは"      # 変えていない
    assert new[3]["text"] == "おわり"
    assert new[0]["start"] == 0.0 and new[1]["end"] == 2.0  # タイムスタンプ保持


def test_polish_bad_reply_keeps_original():
    # 行数を崩した応答 → そのバッチは原文採用（内容が失われない）
    correct.groq_client.chat_complete = lambda messages, **kw: "1: 勝手に1行だけ"
    new, _ = correct.polish_segments(_segs(), "ja", None)
    assert [s["text"] for s in new] == ["こんにちは", "せかい", "", "おわり"]


def test_polish_exception_keeps_original():
    def boom(messages, **kw):
        raise correct.groq_client.GroqError(500, "server error")
    correct.groq_client.chat_complete = boom
    new, _ = correct.polish_segments(_segs(), "ja", None)
    assert [s["text"] for s in new] == ["こんにちは", "せかい", "", "おわり"]


def test_max_batches_guard():
    correct.groq_client.chat_complete = _fake_good
    bb, mb = correct.BATCH_CHARS, correct.MAX_BATCHES
    try:
        correct.BATCH_CHARS = 6
        correct.MAX_BATCHES = 1   # 1バッチだけ校正、残りは原文＋警告
        new, note = correct.polish_segments(_segs(), "ja", None)
        assert note is not None              # 警告が出る
        assert new[0]["text"] == "こんにちは"  # 先頭バッチは処理（変化なし語）
    finally:
        correct.BATCH_CHARS, correct.MAX_BATCHES = bb, mb


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
