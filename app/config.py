"""アプリ設定。環境変数（または .env ファイル）から読み込む。"""
import os
import secrets

from dotenv import load_dotenv

# プロジェクト直下の .env を読み込む（無ければ無視）。
# 日本語コメント入り .env を Windows でも確実に読むため encoding を明示。
load_dotenv(encoding="utf-8")


def _clean(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


# --- Groq (文字起こしエンジン) ---
GROQ_API_KEY = _clean("GROQ_API_KEY")
# whisper-large-v3 = 日本語精度が高い / whisper-large-v3-turbo = 高速・低コスト
GROQ_MODEL = _clean("GROQ_MODEL", "whisper-large-v3")
GROQ_BASE_URL = _clean("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# --- 文字起こしの既定言語 ("ja"=日本語, "en"=英語, ""=自動判定) ---
DEFAULT_LANGUAGE = _clean("DEFAULT_LANGUAGE", "ja")

# --- パスワード認証（複数人運用）---
# APP_PASSWORD: 共有パスワード（1つ）
# APP_PASSWORDS: カンマ区切りで複数のパスワードを許可（配布・無効化しやすい）
_passwords: set[str] = set()
if _clean("APP_PASSWORD"):
    _passwords.add(_clean("APP_PASSWORD"))
for _p in _clean("APP_PASSWORDS").split(","):
    _p = _p.strip()
    if _p:
        _passwords.add(_p)

# パスワード未設定なら、誰でも入れる状態を防ぐためランダム生成して起動ログに表示する
AUTH_DISABLED_WARNING = False
if not _passwords:
    AUTH_DISABLED_WARNING = True
    _passwords.add(secrets.token_urlsafe(9))

PASSWORDS: frozenset[str] = frozenset(_passwords)

# セッション署名鍵（未設定なら毎起動でランダム＝再起動で再ログインが必要になるだけ）
SECRET_KEY = _clean("SECRET_KEY") or secrets.token_hex(32)

# --- 制限・処理パラメータ ---
MAX_UPLOAD_MB = int(_clean("MAX_UPLOAD_MB", "300"))      # アップロード上限
CHUNK_SECONDS = int(_clean("CHUNK_SECONDS", "600"))       # 何秒ごとに分割するか（10分）
TC_INTERVAL = float(_clean("TC_INTERVAL_SECONDS", "10"))  # 本文中にTCを入れる目安間隔（秒）
TARGET_SR = int(_clean("TARGET_SR", "16000"))            # 文字起こし用サンプルレート
AUDIO_BITRATE = _clean("AUDIO_BITRATE", "32k")           # 圧縮ビットレート（mp3 mono）

# 受け付ける拡張子（音声＋動画）
ALLOWED_EXT = {
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma",
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".3gp",
}
