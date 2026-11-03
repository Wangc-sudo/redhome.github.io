"""本地 CSV/JSON/截图 → ZOS；先上传并计算 SHA256，再回传对象索引。"""
from __future__ import annotations

import hashlib
from pathlib import Path


class ArtifactUploadError(RuntimeError):
    pass


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_key(prefix, captured_at, job_id, filename):
    safe_name = Path(filename).name
    return f"{prefix.strip('/')}/{captured_at:%Y/%m/%d}/{job_id}/{safe_name}"


def upload_artifacts(local_artifacts, *, config, job_id, captured_at, oss_config):
    required = ("access_key_id", "access_key_secret", "endpoint", "bucket")
    if any(not oss_config.get(key) for key in required):
        raise ArtifactUploadError("zos_config_incomplete")
    if oss_config["bucket"] != config["artifacts"]["bucket"]:
        raise ArtifactUploadError("zos_bucket_mismatch")
    try:
        import boto3
        from botocore.config import Config
        client = boto3.client(
            "s3",
            endpoint_url=oss_config["endpoint"],
            aws_access_key_id=oss_config["access_key_id"],
            aws_secret_access_key=oss_config["access_key_secret"],
            region_name=oss_config.get("region", "huadong-1"),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        uploaded = []
        for item in local_artifacts:
            path = Path(item["path"])
            key = object_key(config["artifacts"]["prefix"], captured_at, job_id, path.name)
            client.upload_file(str(path), oss_config["bucket"], key)
            uploaded.append({
                "subject_key": item.get("subject_key") or "",
                "artifact_type": item["artifact_type"],
                "bucket": oss_config["bucket"],
                "object_key": key,
                "sha256": file_sha256(path),
                "size_bytes": path.stat().st_size,
                "upload_status": "uploaded",
            })
        return uploaded
    except ArtifactUploadError:
        raise
    except Exception as exc:
        raise ArtifactUploadError("zos_upload_failed") from exc
