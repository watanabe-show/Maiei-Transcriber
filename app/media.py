"""ffmpeg を使った音声抽出・圧縮・分割。

動画(mp4/mov等)からは音声トラックだけを取り出し、
すべて 16kHz mono mp3 に圧縮してから、一定秒数ごとに分割する。
Windows でも ffmpeg の手動インストールは不要（imageio-ffmpeg が同梱）。
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess

from . import config

_FFMPEG_CACHE: str | None = None


def ffmpeg_exe() -> str:
    """利用する ffmpeg 実行ファイルのパスを返す。

    1) PATH 上に ffmpeg があればそれを使う（Docker/Render 等）
    2) なければ imageio-ffmpeg 同梱バイナリを使う（Windows ローカル等）
    """
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE

    system = shutil.which("ffmpeg")
    if system:
        _FFMPEG_CACHE = system
        return system

    import imageio_ffmpeg

    _FFMPEG_CACHE = imageio_ffmpeg.get_ffmpeg_exe()
    return _FFMPEG_CACHE


def transcode_and_segment(
    input_path: str,
    out_dir: str,
    chunk_seconds: int = config.CHUNK_SECONDS,
    target_sr: int = config.TARGET_SR,
    bitrate: str = config.AUDIO_BITRATE,
) -> list[str]:
    """入力ファイルを 16kHz mono mp3 に変換し、chunk_seconds ごとに分割する。

    返り値: 分割された mp3 ファイルパスの昇順リスト。
    ファイルが短ければ 1 個だけ返る。
    """
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "chunk_%04d.mp3")

    cmd = [
        ffmpeg_exe(),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]
    # ネットワーク入力（R2のpresigned URL等）は途中切断に備えて再接続を有効化する。
    if input_path.startswith(("http://", "https://")):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    cmd += [
        "-i", input_path,
        "-vn",                       # 映像トラックを捨てる（動画→音声のみ）
        "-ac", "1",                  # モノラル
        "-ar", str(target_sr),       # 16kHz
        "-c:a", "libmp3lame",
        "-b:a", bitrate,             # 例: 32k（音声には十分・超軽量）
        "-f", "segment",
        "-segment_time", str(chunk_seconds),
        pattern,
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-1500:]
        raise RuntimeError(
            "音声の変換に失敗しました。ファイルが壊れているか、対応していない形式の可能性があります。\n"
            f"ffmpeg: {tail}"
        )

    files = sorted(glob.glob(os.path.join(out_dir, "chunk_*.mp3")))
    if not files:
        raise RuntimeError("音声トラックが見つかりませんでした（無音、または映像のみのファイルの可能性があります）。")
    return files
