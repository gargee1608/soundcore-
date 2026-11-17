import hashlib
import os
from pathlib import Path

import boto3
from moto import mock_aws

from soundcore.db import session_scope
from soundcore.models import Recording, S3Object, StorageState
from soundcore.storage.offload import offload_recording
from soundcore.storage.s3 import S3Uploader, VerifyResult
from tests.conftest import add_channel, add_recording


class MismatchClient:
    def put_object(self, **kwargs):
        body = kwargs["Body"]
        self.body = body.read() if hasattr(body, "read") else body

    def head_object(self, **kwargs):
        return {"ETag": '"deadbeef"', "ContentLength": 1}


def test_mismatch_keeps_the_local_file(data_dir, now):
    with session_scope() as session:
        channel = add_channel(session)
        recording = add_recording(
            session,
            data_dir,
            channel,
            state=StorageState.LOCAL_ONLY,
            started_at=now,
            ended_at=now,
            name="keep.wav",
            body=b"not-verified",
        )
        path = data_dir / recording.local_path
        uploader = S3Uploader(MismatchClient(), bucket="soundcore-test", prefix="soundcore")
        outcome = offload_recording(session, recording, uploader, data_dir)
        session.refresh(recording)
        assert outcome == "kept"
        assert path.read_bytes() == b"not-verified"
        assert recording.storage_state == StorageState.UPLOADED_UNVERIFIED
        assert session.query(S3Object).count() == 0


@mock_aws
def test_verified_upload_then_local_delete(data_dir, now):
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="soundcore-test")
    body = b"hello-soundcore-audio"
    with session_scope() as session:
        channel = add_channel(session)
        recording = add_recording(
            session,
            data_dir,
            channel,
            state=StorageState.LOCAL_ONLY,
            started_at=now,
            ended_at=now,
            name="ok.wav",
            body=body,
        )
        path = data_dir / recording.local_path
        uploader = S3Uploader(client, bucket="soundcore-test", prefix="soundcore")
        outcome = offload_recording(session, recording, uploader, data_dir)
        session.refresh(recording)
        assert outcome == "deleted"
        assert not path.exists()
        assert recording.storage_state == StorageState.LOCAL_DELETED
        s3_row = session.query(S3Object).filter_by(recording_id=recording.id).one()
        assert s3_row.verified_at is not None
        assert s3_row.size_bytes == len(body)
        assert s3_row.etag == hashlib.md5(body).hexdigest()
        assert recording.s3_object_id == s3_row.id
        remote = client.get_object(Bucket="soundcore-test", Key=s3_row.key)
        assert remote["Body"].read() == body
        stored = session.get(Recording, recording.id)
        assert stored.storage_state == StorageState.LOCAL_DELETED


def test_verify_result_rejects_multipart_etag():
    from soundcore.storage.s3 import etag_matches

    assert etag_matches("abc", '"abc"') is True
    assert etag_matches("abc", "abc-2") is False
    assert VerifyResult(ok=False).ok is False
