"""実サーバー(uvicorn)に対するエンドツーエンド検証。
login → upload → poll → terminal までブラウザと同じHTTP経路で確認する。
"""
import os
import subprocess
import sys
import tempfile
import time

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8011")
PW = os.environ.get("APP_PASSWORD", "test123")

# imageio-ffmpeg でテスト音声を生成
import imageio_ffmpeg

ff = imageio_ffmpeg.get_ffmpeg_exe()
work = tempfile.mkdtemp(prefix="live_")
tone = os.path.join(work, "tone.wav")
subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=4", tone], check=True)

ok_all = True


def show(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"  [{'OK' if cond else 'NG'}] {name}" + (f"  ({extra})" if extra else ""))


with httpx.Client(base_url=BASE, timeout=30) as c:
    r = c.post("/api/login", data={"password": PW})
    show("ログイン", r.status_code == 200, f"status={r.status_code}")

    with open(tone, "rb") as fh:
        r = c.post("/api/transcribe",
                   files={"file": ("tone.wav", fh, "audio/wav")},
                   data={"language": "ja"})
    show("アップロード→job_id取得", r.status_code == 200 and "job_id" in r.json(),
         f"status={r.status_code}")
    job_id = r.json().get("job_id")

    terminal = None
    stages = []
    for _ in range(60):
        time.sleep(0.5)
        jr = c.get(f"/api/jobs/{job_id}").json()
        stages.append((jr.get("progress"), jr.get("stage")))
        if jr.get("status") in ("done", "error"):
            terminal = jr
            break
    show("進捗ポーリングで終了状態に到達(=本番の非同期処理が動作)", terminal is not None,
         "timeout" if terminal is None else terminal.get("status"))
    if terminal:
        either = terminal["status"] == "done" or (
            terminal["status"] == "error" and "GROQ_API_KEY" in (terminal.get("error") or ""))
        show("ffmpeg変換→Groq呼び出しまで到達", either,
             terminal.get("error") or f"segments={len(terminal.get('segments') or [])}")
    print("  進捗の遷移:", " -> ".join(f"{p}%" for p, _ in stages[:8]))

print("RESULT:", "ALL OK" if ok_all else "HAS FAILURE")
sys.exit(0 if ok_all else 1)
