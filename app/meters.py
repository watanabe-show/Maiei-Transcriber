"""使用量の月次台帳（Groqの文字起こし時間 / Gladiaの話者分離時間）。

「今月どれだけ使ったか」を秒で積み上げて保存する。ジョブが終わるたびに書く
（タイマーで定期書き込みはしない＝無料枠のRenderは夜間スリープするため、
プロセスが死ぬ前に確実に書けている必要がある）。

保存先は2通り。R2(S3互換)が設定されていればそちら、無ければローカルのJSONファイル。
Renderの無料枠には永続ディスクが無いのでローカル保存はスリープ・再デプロイで消えるが、
ローカル起動では問題なく残る。

**この台帳の失敗は絶対にジョブを止めない**（使用量の表示のために文字起こしを
落とさない）。読み書きの例外は握りつぶしてログだけ出す。
ただし Gladia の無料枠ハード遮断（reserve）だけは例外で、読めなければ
「使い切っている」側に倒して止める（無料枠を1秒も超えないため）。
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

# 台帳に記録するサービス（キーは "<service>_seconds" になる）
SERVICES = ("groq", "gladia")

# 読み書きを直列化する（ジョブは同時2本まで走るため read-modify-write が競合しうる）
_LOCK = asyncio.Lock()


def month_key(now: datetime | None = None) -> str:
    """当月のキー 'YYYY-MM'（日本時間）。"""
    return (now or datetime.now(JST)).astimezone(JST).strftime("%Y-%m")


def backend() -> str:
    """いま使っている保存先の名前（UIの但し書き用）。"""
    return "r2" if storage.enabled() else "local"


def _empty(ym: str) -> dict:
    data = {"year_month": ym}
    for s in SERVICES:
        data[f"{s}_seconds"] = 0.0
    return data


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
    out = _empty(ym)
    out["year_month"] = str(data.get("year_month") or ym)
    for s in SERVICES:
        out[f"{s}_seconds"] = float(data.get(f"{s}_seconds") or 0.0)
    return out


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


async def add_seconds(service: str, seconds: float) -> None:
    """使った秒数を当月へ加算する（上限のないサービス＝Groq用）。

    「差分の加算」であって上書きではない（プロセスが再起動しても積み上がりが消えない）。
    """
    if not seconds or seconds <= 0:
        return
    await _apply(service, float(seconds))


async def adjust(service: str, delta_seconds: float) -> None:
    """予約した秒数を実績へ補正する（差分。マイナス可）。"""
    if not delta_seconds:
        return
    await _apply(service, float(delta_seconds))


async def _apply(service: str, delta: float) -> None:
    key = f"{service}_seconds"
    ym = month_key()
    try:
        async with _LOCK:
            data = await asyncio.to_thread(_load_sync, ym)
            data[key] = max(0.0, float(data.get(key) or 0.0) + delta)
            data["year_month"] = ym
            await asyncio.to_thread(_save_sync, ym, data)
    except Exception as exc:
        print(f"[meters] 書き込みに失敗しました（使用量の表示だけがずれます）: {exc}")


async def try_reserve(service: str, seconds: float, limit_seconds: float) -> tuple[bool, float]:
    """上限つきサービス（Gladia）の枠を「先に確保」する。

    残量チェックと加算を**同じロックの中で**行うのが要点。別々にすると、
    ジョブが2本同時に走ったとき（`jobs._SEMAPHORE` は2）両方が残量チェックを
    通ってしまい、合計で上限を超える。

    返り値: (確保できたか, 確保後の使用秒数)。確保できなかった場合の第2要素は現在の使用秒数。
    台帳が読めないときは False（使い切っている側に倒す）＝無料枠を1秒も超えないため。
    """
    key = f"{service}_seconds"
    ym = month_key()
    try:
        async with _LOCK:
            data = await asyncio.to_thread(_load_sync, ym)
            used = float(data.get(key) or 0.0)
            if used + seconds > limit_seconds:
                return False, used
            data[key] = used + seconds
            data["year_month"] = ym
            await asyncio.to_thread(_save_sync, ym, data)
            return True, used + seconds
    except Exception as exc:
        print(f"[meters] 残量を確認できませんでした（安全側に倒して停止します）: {exc}")
        return False, limit_seconds
