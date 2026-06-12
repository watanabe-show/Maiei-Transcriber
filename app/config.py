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

# --- 句読点プライミング（prompt）---
# Whisperは prompt の文体を真似る性質があり、句読点付きの例文を渡すと
# 出力にも句読点が入りやすくなる。「一文ごと」の区切り精度を底上げする。
# 言語自動判定(auto)時は言語を誤検出させないよう prompt を渡さない。
PUNCT_PROMPT_JA = _clean(
    "PUNCT_PROMPT_JA",
    "以下は、日本語の音声を句読点を付けて書き起こしたものです。"
    "こんにちは。今日は、よろしくお願いします。それでは、始めます。",
)
PUNCT_PROMPT_EN = _clean(
    "PUNCT_PROMPT_EN",
    "The following is a transcript with proper punctuation. "
    "Hello. Thanks for joining today. Let's get started.",
)


def punct_prompt(language: str | None) -> str | None:
    """言語に応じた句読点プライミング用 prompt を返す（auto/不明なら None）。"""
    if language == "ja":
        return PUNCT_PROMPT_JA or None
    if language == "en":
        return PUNCT_PROMPT_EN or None
    return None

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

# --- 大容量動画むけ：クラウドストレージ直アップロード（任意機能 / S3互換=R2想定）---
# 設定すると、大きいファイルはブラウザ→R2へ直接アップし、サーバーは presigned URL から
# ffmpeg でストリーミング変換する。未設定なら従来どおりサーバー直アップロードのみで動く。
S3_ENDPOINT = _clean("S3_ENDPOINT")              # 例: https://<accountid>.r2.cloudflarestorage.com
S3_BUCKET = _clean("S3_BUCKET")
S3_ACCESS_KEY_ID = _clean("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = _clean("S3_SECRET_ACCESS_KEY")
S3_REGION = _clean("S3_REGION", "auto")
# 4つの必須値が揃っていれば直アップロード機能が有効になる
STORAGE_ENABLED = bool(S3_ENDPOINT and S3_BUCKET and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY)

STORAGE_MAX_UPLOAD_MB = int(_clean("STORAGE_MAX_UPLOAD_MB", "3000"))  # 直アップロードの上限(MB)
DIRECT_UPLOAD_MAX_MB = int(_clean("DIRECT_UPLOAD_MAX_MB", "200"))     # これ以下は従来のサーバー直アップを使う閾値
UPLOAD_PART_MB = int(_clean("UPLOAD_PART_MB", "64"))                  # multipart 1パートの大きさ(MB / 最小5)
PRESIGN_PUT_EXPIRE = int(_clean("PRESIGN_PUT_EXPIRE", "21600"))       # パートPUT URLの有効期限(秒)=6h
PRESIGN_GET_EXPIRE = int(_clean("PRESIGN_GET_EXPIRE", "21600"))       # GET URLの有効期限(秒)=6h

# 受け付ける拡張子（音声＋動画）
ALLOWED_EXT = {
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma",
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".3gp",
}
