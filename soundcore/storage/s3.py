"""Upload to S3, then prove the object matches the local file before anyone may delete it."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import boto3


VERIFY_METHOD = "etag_md5_and_size"


@dataclass
class VerifyResult:
    ok: bool
    uploaded: bool = False
    etag: str = ""
    remote_size: int | None = None
    local_size: int = 0
    local_md5: str = ""
    stage: str = ""
    error: str = ""
    verify_method: str = VERIFY_METHOD


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_etag(etag: str | None) -> str:
    if not etag:
        return ""
    return etag.strip().strip('"')


def s3_key_for(prefix: str, relative_path: str) -> str:
    rel = relative_path.replace("\\", "/").lstrip("/")
    pre = (prefix or "").strip("/")
    return f"{pre}/{rel}" if pre else rel


def etag_matches(local_md5: str, remote_etag: str) -> bool:
    etag = normalize_etag(remote_etag)
    # Multipart ETags look like "<md5>-<parts>" and are not the file MD5.
    if not etag or "-" in etag:
        return False
    return etag.lower() == local_md5.lower()


class S3Uploader:
    def __init__(self, client, *, bucket: str, prefix: str = "soundcore"):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    @classmethod
    def from_settings(cls, settings) -> "S3Uploader":
        client = boto3.client(
            "s3",
            region_name=settings.s3_region or "us-east-1",
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
            endpoint_url=settings.s3_endpoint_url or None,
        )
        return cls(client, bucket=settings.s3_bucket, prefix=settings.s3_prefix)

    def key_for(self, relative_path: str) -> str:
        return s3_key_for(self.prefix, relative_path)

    def upload_and_verify(self, local_path: Path, key: str | None = None) -> VerifyResult:
        path = Path(local_path)
        if not path.is_file():
            return VerifyResult(ok=False, stage="local", error="local file missing")
        local_size = path.stat().st_size
        local_md5 = file_md5(path)
        object_key = key or path.name
        try:
            with path.open("rb") as handle:
                self.client.put_object(Bucket=self.bucket, Key=object_key, Body=handle)
        except Exception as exc:  # boto errors vary; verification must fail closed
            return VerifyResult(
                ok=False,
                uploaded=False,
                stage="upload",
                error=str(exc),
                local_size=local_size,
                local_md5=local_md5,
            )
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            return VerifyResult(
                ok=False,
                uploaded=True,
                stage="head",
                error=str(exc),
                local_size=local_size,
                local_md5=local_md5,
            )
        remote_size = int(head.get("ContentLength") or 0)
        etag = normalize_etag(head.get("ETag"))
        if remote_size != local_size or not etag_matches(local_md5, etag):
            return VerifyResult(
                ok=False,
                uploaded=True,
                stage="verify",
                error="etag or size mismatch",
                etag=etag,
                remote_size=remote_size,
                local_size=local_size,
                local_md5=local_md5,
            )
        return VerifyResult(
            ok=True,
            uploaded=True,
            etag=etag,
            remote_size=remote_size,
            local_size=local_size,
            local_md5=local_md5,
            stage="verified",
        )

    def presign_get(self, key: str, expires: int = 3600) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )
