"""Cloudflare R2（S3互換）への multipart アップロードと presigned URL 発行。

大容量動画は「ブラウザ → R2 へ直接アップロード」し、サーバーは presigned GET URL を
ffmpeg にストリーミング入力する（Renderの小さなディスク/メモリを大きなファイルで圧迫しない）。

R2 が未設定（環境変数が無い）なら enabled() が False を返し、アプリは従来どおり
サーバー直アップロードのみで動作する（この機能は完全に任意）。

boto3 を使う。R2 以外でも S3互換ストレージ（Backblaze B2 / AWS S3 等）なら
S3_ENDPOINT を変えるだけで利用できる。
"""
from __future__ import annotations

import uuid

from . import config

_CLIENT = None


def enabled() -> bool:
    """必要な環境変数が揃っていれば True。"""
    return config.STORAGE_ENABLED


def _client():
    """boto3 S3クライアントを遅延生成（未インストールならここで例外）。"""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    import boto3
    from botocore.config import Config as BotoConfig

    # boto3>=1.36 は既定でアップロードにチェックサムを付与するが、これがブラウザからの
    # presigned PUT で署名不一致(SignatureDoesNotMatch)を起こす。R2でも踏むため無効化する。
    # 古い botocore には当該オプションが無いので、その場合はフォールバックする。
    try:
        cfg = BotoConfig(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
    except TypeError:
        cfg = BotoConfig(signature_version="s3v4")

    _CLIENT = boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT,
        aws_access_key_id=config.S3_ACCESS_KEY_ID,
        aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
        region_name=config.S3_REGION or "auto",
        config=cfg,
    )
    return _CLIENT


def new_key(ext: str) -> str:
    """ランダムなオブジェクトキーを作る（uploads/ 配下に限定）。"""
    ext = ext if ext.startswith(".") else (("." + ext) if ext else "")
    return f"uploads/{uuid.uuid4().hex}{ext}"


def is_valid_key(key: str) -> bool:
    """クライアントから渡されたキーが想定の領域内かを軽く検証する。"""
    return bool(key) and key.startswith("uploads/") and ".." not in key


def initiate(key: str, content_type: str = "application/octet-stream") -> str:
    """multipart アップロードを開始し UploadId を返す。"""
    resp = _client().create_multipart_upload(
        Bucket=config.S3_BUCKET, Key=key, ContentType=content_type
    )
    return resp["UploadId"]


def presign_parts(key: str, upload_id: str, part_numbers, expires: int) -> dict[int, str]:
    """指定パート番号の presigned PUT URL を発行する（再送/再開の再発行にも使う）。"""
    cl = _client()
    out: dict[int, str] = {}
    for n in part_numbers:
        n = int(n)
        out[n] = cl.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": config.S3_BUCKET,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": n,
            },
            ExpiresIn=expires,
        )
    return out


def presign_get(key: str, expires: int) -> str:
    """ffmpeg 入力用の presigned GET URL を発行する。"""
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": config.S3_BUCKET, "Key": key},
        ExpiresIn=expires,
    )


def complete(key: str, upload_id: str, parts: list[dict]) -> None:
    """全パートの ETag を集めて multipart を確定する。

    parts: [{"PartNumber": int, "ETag": str}, ...]（順不同で渡されてもよい）
    """
    norm = sorted(
        ({"PartNumber": int(p["PartNumber"]), "ETag": p["ETag"]} for p in parts),
        key=lambda p: p["PartNumber"],
    )
    _client().complete_multipart_upload(
        Bucket=config.S3_BUCKET,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": norm},
    )


def abort(key: str, upload_id: str) -> None:
    """未完了の multipart を中断する（失敗しても無視）。"""
    try:
        _client().abort_multipart_upload(
            Bucket=config.S3_BUCKET, Key=key, UploadId=upload_id
        )
    except Exception:
        pass


def delete(key: str) -> None:
    """オブジェクトを削除する（処理後の後始末。失敗しても無視）。"""
    try:
        _client().delete_object(Bucket=config.S3_BUCKET, Key=key)
    except Exception:
        pass
