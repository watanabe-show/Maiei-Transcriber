"""簡易スモークテスト。実際のGroq呼び出し以外を一通り検証する。

実行: .venv の python で  python scripts/smoke_test.py
"""
import os
import subprocess
import sys
import tempfile
import time

# 設定を import より前に注入（GROQキーは空＝API手前まで検証）
os.environ.setdefault("GROQ_API_KEY", "")
os.environ.setdefault("APP_PASSWORD", "test123")
os.environ.setdefault("DEFAULT_LANGUAGE", "ja")

# R2(S3互換)を必ず無効にする。.env に本番のキーが入っている環境でテストを走らせると、
# 使用量台帳が本番バケットへ書き込まれてしまうため（load_dotenv は既存の環境変数を
# 上書きしないので、空文字を先に置けばローカルJSON側の経路が使われる）。
for _k in ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
    os.environ[_k] = ""

# Gladiaのキーも必ず空にする（実際のAPIを叩かない／トグル非表示の確認もするため）
os.environ["GLADIA_API_KEY"] = ""

# プロジェクトルートを import パスに追加
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import media, formats  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASS, FAIL = "  [OK]", "  [NG]"
results = []


def check(name, cond, extra=""):
    line = f"{PASS if cond else FAIL} {name}" + (f"  ({extra})" if extra else "")
    print(line)
    results.append(bool(cond))
    return cond


print("=== 1. ffmpeg ===")
ff = media.ffmpeg_exe()
check("ffmpeg 実行ファイルが見つかる", os.path.exists(ff), ff)

work = tempfile.mkdtemp(prefix="smoke_")
tone = os.path.join(work, "tone.wav")
subprocess.run(
    [ff, "-hide_banner", "-loglevel", "error", "-y",
     "-f", "lavfi", "-i", "sine=frequency=440:duration=3", tone],
    check=True,
)
check("テスト音声(3秒)を生成", os.path.exists(tone) and os.path.getsize(tone) > 0)

print("=== 2. 変換＆分割 (1秒ごと→3チャンク想定) ===")
seg_dir = os.path.join(work, "chunks")
files = media.transcode_and_segment(tone, seg_dir, chunk_seconds=1)
check("mp3チャンクが複数生成される", len(files) >= 2, f"{len(files)}個")
check("各チャンクが0バイト超", all(os.path.getsize(f) > 0 for f in files))

print("=== 3. 出力フォーマット ===")
segs = [
    {"start": 0.0, "end": 2.5, "text": "こんにちは"},
    {"start": 2.5, "end": 5.0, "text": "テストです"},
    {"start": 9.0, "end": 11.0, "text": "別の段落です"},  # 4秒の間→新段落
]
paras = formats.group_paragraphs(segs)
check("無音の間で段落が分かれる(2段落)", len(paras) == 2, f"{len(paras)}段落")

blocks10 = formats.group_time_blocks(
    [{"start": 0, "end": 3, "text": "あ"},
     {"start": 4, "end": 7, "text": "い"},
     {"start": 12, "end": 14, "text": "う"}], interval=10)
check("約10秒ごとにTCブロック分割(2ブロック)", len(blocks10) == 2, f"{len(blocks10)}ブロック")

srt, ext, mime = formats.render_text(segs, "srt")
check("SRTにタイムスタンプ行が含まれる", "00:00:00,000 --> 00:00:02,500" in srt, ext)
vtt, _, _ = formats.render_text(segs, "vtt")
check("VTTヘッダがある", vtt.startswith("WEBVTT"))
txt, _, _ = formats.render_text(segs, "txt")
check("整形txtが本文を含む", "こんにちは" in txt and "別の段落です" in txt)
tts, _, _ = formats.render_text(segs, "txt_ts")
check("時間つきtxtに[00:00:00]が付く", "[00:00:00]" in tts)

from app import documents  # noqa: E402
docx_bytes = documents.build_docx(paras, title="テスト")
check("Word(.docx)が生成される(zip/PK署名)", docx_bytes[:2] == b"PK" and len(docx_bytes) > 2000)
xlsx_bytes = documents.build_xlsx(paras, segs)
check("Excel(.xlsx)が生成される(zip/PK署名)", xlsx_bytes[:2] == b"PK" and len(xlsx_bytes) > 2000)

print("=== 4. HTTP エンドポイント ===")
client = TestClient(app)

r = client.get("/api/me")
check("/api/me 未ログインは authed=false", r.json().get("authed") is False)

r = client.get("/")
check("/ 未ログインはログイン画面", "パスワード" in r.text)

r = client.post("/api/login", data={"password": "wrong"})
check("誤った合言葉は401", r.status_code == 401)

r = client.post("/api/login", data={"password": "test123"})
check("正しい合言葉でログイン成功", r.status_code == 200 and r.json().get("ok") is True)

r = client.get("/")
check("ログイン後はアプリ画面(ドロップゾーン)", "ドラッグ" in r.text)

# アップロード→ジョブ作成
with open(tone, "rb") as fh:
    r = client.post(
        "/api/transcribe",
        files={"file": ("tone.wav", fh, "audio/wav")},
        data={"language": "ja"},
    )
ok = r.status_code == 200 and "job_id" in r.json()
check("アップロードでジョブ作成", ok, f"status={r.status_code}")

print("=== 5. ジョブ処理パイプライン（直接実行） ===")
import asyncio  # noqa: E402
import shutil  # noqa: E402

from app import jobs  # noqa: E402

pj_work = tempfile.mkdtemp(prefix="job_")
pj_input = os.path.join(pj_work, "input.wav")
shutil.copy(tone, pj_input)
jid = jobs.create_job("tone.wav", pj_work, pj_input, "ja")
asyncio.run(jobs._run(jid))
job = jobs.JOBS[jid]
check("パイプラインが終了状態に到達", job["status"] in ("done", "error"), job["status"])
# キーがあれば done、無ければ GROQ_API_KEY エラー（どちらも想定どおり）
either = job["status"] == "done" or (
    job["status"] == "error" and "GROQ_API_KEY" in (job.get("error") or "")
)
check("ffmpeg変換→Groq呼び出しまで到達", either,
      job.get("error") or f"segments={len(job.get('segments') or [])}")

# 不正拡張子
r = client.post("/api/transcribe",
                files={"file": ("bad.txt", b"hello", "text/plain")},
                data={"language": "ja"})
check("非対応拡張子は400で拒否", r.status_code == 400)

print("=== 6. 切り方（一文/秒ごと/段落/TimeCodeなし）===")
# 一文ごと: 文末記号で区切る（「今日は」＋「晴れです。」は1文に連結される）
segs_sent = [
    {"start": 0, "end": 2, "text": "おはよう。"},
    {"start": 2, "end": 4, "text": "今日は"},
    {"start": 4, "end": 6, "text": "晴れです。"},
    {"start": 6, "end": 8, "text": "そうですね。"},
]
sent = formats.group_sentences(segs_sent)
check("一文ごとに文末で区切る(3文)", len(sent) == 3, f"{len(sent)}文")

# 約6秒間隔の素材：5秒ごとは30秒ごとより細かく割れる
segs_time = [{"start": i * 6, "end": i * 6 + 2, "text": f"文{i}。"} for i in range(6)]
views = formats.build_views(segs_time)
check("build_viewsが全ての切り方を返す", set(views) == set(formats.GRAN_LABELS),
      f"{len(views)}種類")
b5 = formats.build_blocks(segs_time, "sec5")
b30 = formats.build_blocks(segs_time, "sec30")
check("5秒ごとは30秒ごとより細かい", len(b5) > len(b30), f"5秒={len(b5)} / 30秒={len(b30)}")

t_plain, _, _ = formats.render_text(segs_time, "txt", "plain")
check("テキスト(TimeCodeなし)に[時刻]が無い", "[00:00:00]" not in t_plain)
t_tc, _, _ = formats.render_text(segs_time, "txt", "sec5")
check("テキスト(5秒ごと)に[時刻]が付く", "[00:00:00]" in t_tc)

docx_plain = documents.build_docx(views["plain"], title="t", show_tc=False)
check("Word(TimeCodeなし)が生成される", docx_plain[:2] == b"PK" and len(docx_plain) > 2000)
xlsx_lbl = documents.build_xlsx(b5, segs_time, body_label="本文（5秒ごと）")
check("Excel(切り方ラベル付)が生成される", xlsx_lbl[:2] == b"PK" and len(xlsx_lbl) > 2000)

print("=== 7. 使用量メーター（今月の文字起こし時間）===")
from app import meters  # noqa: E402

check("テスト中はR2を使わない(ローカル保存)", meters.backend() == "local", meters.backend())

import re  # noqa: E402

check("月キーが YYYY-MM 形式", bool(re.fullmatch(r"\d{4}-\d{2}", meters.month_key())),
      meters.month_key())

# 台帳の実体はテスト用の一時ディレクトリへ逃がす（プロジェクトを汚さない）
meters.LOCAL_DIR = os.path.join(work, "meters")
asyncio.run(meters.add_seconds("groq", 90))
asyncio.run(meters.add_seconds("groq", 30))
led = asyncio.run(meters.read())
check("加算が積み上がる(上書きでなく差分加算)", abs(led["groq_seconds"] - 120.0) < 0.01,
      f"{led['groq_seconds']}秒")

r = client.get("/api/usage")
check("/api/usage が今月の秒数を返す",
      r.status_code == 200 and r.json().get("groq_seconds") == 120.0,
      f"status={r.status_code} body={r.text[:80]}")

print("=== 8. 話者分離（Gladia経路の整形・遮断ロジック）===")
from app import gladia_client  # noqa: E402

# --- 応答の整形 ---
fake_result = {
    "transcription": {"utterances": [
        {"speaker": 0, "start": 0.0, "end": 3.0, "text": "こんばんは。"},
        {"speaker": 1, "start": 3.2, "end": 6.0, "text": "よろしくお願いします。"},
        {"speaker": 0, "start": 6.2, "end": 9.0, "text": "本日のテーマです。"},
        {"speaker": 4, "start": 9.1, "end": 9.4, "text": "ええ"},          # 過検出の断片
    ]},
    "metadata": {"billing_time": 840.0},
}
gsegs = gladia_client.to_segments(fake_result)
check("utterancesをsegments形に直せる", len(gsegs) == 4 and gsegs[0]["speaker"] == 0,
      f"{len(gsegs)}件")
check("billing_timeを実績として取れる", gladia_client.billing_seconds(fake_result) == 840.0)

merged = gladia_client.merge_minor_speakers(gsegs, keep=2)
check("過検出の少数話者が上位2人へ統合される",
      len({s["speaker"] for s in merged}) == 2, f"{sorted({s['speaker'] for s in merged})}")

# --- 話者が変わったら必ずブロックが切れる ---
blocks = formats.build_blocks(merged, "para_meaning")
check("話者が変わるとブロックが分かれる", len(blocks) >= 3, f"{len(blocks)}ブロック")
check("各ブロックに話者が付く", all(b.get("speaker") is not None for b in blocks))

# --- 出力に話者ラベルが載る ---
d_txt, _, _ = formats.render_text(merged, "txt", "plain")
check("テキストに「話者1：」が付く", "話者1：" in d_txt, d_txt[:40].replace("\n", " "))
d_srt, _, _ = formats.render_text(merged, "srt")
check("字幕(srt)にも話者が付く", "話者" in d_srt)
d_docx = documents.build_docx(formats.build_blocks(merged, "plain"), title="話者テスト", show_tc=False)
check("Word(話者つき)が生成される", d_docx[:2] == b"PK" and len(d_docx) > 2000)
d_xlsx = documents.build_xlsx(blocks, merged, body_label="本文")
check("Excel(話者つき)が生成される", d_xlsx[:2] == b"PK" and len(d_xlsx) > 2000)

# --- 話者なしの結果は従来と1文字も変わらない（退行防止）---
check("話者なしの出力は従来どおり",
      formats.render_text(segs, "txt", "plain")[0] == formats.to_readable_text(
          formats.group_paragraphs(segs)))

# --- 無料枠のハード遮断 ---
LIMIT = 3600.0   # テスト用に1時間
ok1, _ = asyncio.run(meters.try_reserve("gladia", 3000, LIMIT))
ok2, used2 = asyncio.run(meters.try_reserve("gladia", 3000, LIMIT))
check("上限内なら確保できる", ok1 is True)
check("上限を超える確保は拒否される", ok2 is False and used2 == 3000.0, f"used={used2}")

asyncio.run(meters.adjust("gladia", -200.0))   # 実績が予約より短かった場合の補正
led2 = asyncio.run(meters.read())
check("実績への補正が効く", abs(led2["gladia_seconds"] - 2800.0) < 0.01,
      f"{led2['gladia_seconds']}秒")

r = client.get("/api/usage")
check("/api/usage が話者分離の残量を返す",
      r.status_code == 200 and abs(r.json().get("gladia_remaining_seconds", -1)
                                   - (36000.0 - 2800.0)) < 0.01,
      r.text[:110])

# --- キー未設定ならトグルを出さない ---
r = client.get("/api/config")
check("キー未設定なら話者分離を出さない",
      r.json().get("diarize_enabled") is False, r.text[:90])

print("\n=== 結果 ===")
print(f"  成功 {sum(results)} / {len(results)}")
sys.exit(0 if all(results) else 1)
