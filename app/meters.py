"""使用量の月次台帳（いまは Groq の文字起こし時間だけ）。

「今月どれだけ文字起こししたか」を秒で積み上げて保存する。ジョブが終わるたびに
1回だけ書く（タイマーで定期書き込みはしない＝Renderの無料枠は夜間スリープするため、
プロセスが死ぬ前に確実に書けている必要がある）。

保存先は2通り。R2(S3互換)が設定されていればそちら、無ければローカルのJSONファイル。
Renderの無料枠には永続ディスクが無いのでローカル保存はスリープ・再デプロイで消えるが、
ローカル起動では問題なく残る。

**この台帳の失敗は絶対にジョブを止めない**（使用量の表示のために文字起こしを
落とさない）。読み書きの例外は握りつぶしてログだけ出す。
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from . import storage

# 「今月」は日本時間で数える（利用者にとっての月替わりに合わせる）
JST = timezone(timedelta(hours=9))

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(PROJECT_DIR, "data", "meters")

# 読み書きを直列化する（ジョブは同時2本まで走るため read-modify-write が競合しうる）
_LOCK = asyncio.Lock()


def month_key(now: datetime | None = None) -> str:
    """当月のキー 'YYYY-MM'（日本時間）。"""
    return (now or datetime.now(JST)).astimezone(JST).strftime("%Y-%m")


def backend() -> str:
    """いま使っている保存先の名前（UIの但し書き用）。"""
    return "r2" if storage.enabled() else "local"


def _empty(ym: str) -> dict:
    return {"year_month": ym, "groq_seconds": 0.0}


# ---------------------------------------------------------------- 保存先ごとの入出力
def _r2_key(ym: str) -> str:
    return f"meters/{ym}.json"


def _load_sync(ym: str) -> dict:
    if storage.enabled():
        raw = storage.get_bytes(_r2_key(ym))
    else:
        path = os.path.join(LOCAL_DIR, f"{ym}.json")
        raw = None
        if os.path.exists(path):
            with open(path, "rb") as fh:
                raw = fh.read()
    if not raw:
        return _empty(ym)
    data = json.loads(raw.decode("utf-8"))
    # 想定外の中身でも落ちないように既定値で埋める
    return {
        "year_month": str(data.get("year_month") or ym),
        "groq_seconds": float(data.get("groq_seconds") or 0.0),
    }


def _save_sync(ym: str, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if storage.enabled():
        storage.put_bytes(_r2_key(ym), raw)
        return
    os.makedirs(LOCAL_DIR, exist_ok=True)
    path = os.path.join(LOCAL_DIR, f"{ym}.json")
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    os.replace(tmp, path)   # 書きかけのファイルを読ませない


# ---------------------------------------------------------------- 公開API
async def read() -> dict:
    """当月の台帳を返す。読めなければゼロを返す（例外は投げない）。"""
    ym = month_key()
    try:
        async with _LOCK:
            return await asyncio.to_thread(_load_sync, ym)
    except Exception as exc:
        print(f"[meters] 読み込みに失敗しました（ゼロとして扱います）: {exc}")
        return _empty(ym)


async def add_groq_seconds(seconds: float) -> None:
    """文字起こしした音声の長さ（秒）を当月へ加算する。

    「差分の加算」であって上書きではない（プロセスが再起動しても積み上がりが消えない）。
    """
    if not seconds or seconds <= 0:
        return
    ym = month_key()
    try:
        async with _LOCK:
            data = await asyncio.to_thread(_load_sync, ym)
            data["groq_seconds"] = float(data.get("groq_seconds") or 0.0) + float(seconds)
            data["year_month"] = ym
            await asyncio.to_thread(_save_sync, ym, data)
    except Exception as exc:
        print(f"[meters] 書き込みに失敗しました（使用量の表示だけがずれます）: {exc}")
