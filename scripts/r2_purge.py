"""R2バケットの中身を一覧／一括削除する運用スクリプト。

使い方（プロジェクト直下で）:
  python scripts/r2_purge.py            # 一覧のみ（安全・既定）
  python scripts/r2_purge.py --purge    # uploads/ 配下を全削除＋未完了の分割アップロードを中断

注意:
  - 削除は元に戻せません。本バケットは「処理用の一時アップロード置き場」想定なので
    通常は空でも問題ありませんが、実行前に一覧で中身を必ず確認してください。
  - 削除はストレージ（10GB枠）を解放します。ただし R2 の「操作回数」枠は月単位の
    累積で、削除しても戻りません（日次リセットの枠はありません）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config, storage  # noqa: E402


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


def main() -> int:
    if not storage.enabled():
        print("R2 未設定です（.env の S3_* を確認）。")
        return 1

    cl = storage._client()
    bucket = config.S3_BUCKET
    do_purge = "--purge" in sys.argv

    # --- オブジェクト一覧 ---
    paginator = cl.get_paginator("list_objects_v2")
    keys: list[str] = []
    total = 0
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            keys.append(obj["Key"])
            total += obj.get("Size", 0)
    print(f"バケット: {bucket}")
    print(f"オブジェクト数: {len(keys)} / 合計サイズ: {_human(total)}")
    for k in keys[:20]:
        print("  -", k)
    if len(keys) > 20:
        print(f"  …ほか {len(keys) - 20} 件")

    # --- 未完了の分割アップロード ---
    mpu = cl.list_multipart_uploads(Bucket=bucket).get("Uploads", []) or []
    print(f"未完了の分割アップロード: {len(mpu)} 件")

    if not do_purge:
        print("\n（一覧のみ）削除するには:  python scripts/r2_purge.py --purge")
        return 0

    # --- 削除実行 ---
    print("\n=== 削除を実行します ===")
    for up in mpu:
        try:
            cl.abort_multipart_upload(Bucket=bucket, Key=up["Key"], UploadId=up["UploadId"])
            print("  中断:", up["Key"])
        except Exception as e:  # noqa: BLE001
            print("  中断失敗:", up["Key"], e)
    deleted = 0
    for k in keys:
        try:
            cl.delete_object(Bucket=bucket, Key=k)
            deleted += 1
        except Exception as e:  # noqa: BLE001
            print("  削除失敗:", k, e)
    print(f"削除完了: {deleted}/{len(keys)} 件 / 分割中断: {len(mpu)} 件")
    print("ストレージ枠は解放されます（操作回数の月次枠は戻りません）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
