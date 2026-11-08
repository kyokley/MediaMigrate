import pytest

from mediamigrate.storage import (
    RemoteConflict,
    matching_remote,
    object_uuid,
    upload_verified,
)


class Client:
    def __init__(self, versions=(), unfinished=()):
        self.versions = list(versions)
        self.unfinished = list(unfinished)

    def list_file_versions(self, bucket, key):
        return self.versions

    def list_unfinished_large_files(self, bucket, key):
        return self.unfinished


def test_matching_remote_requires_exact_checksum_and_key():
    good = {"fileName": "a", "action": "upload", "contentLength": 3, "contentSha1": "abc"}
    assert matching_remote(Client([good]), "b", "a", 3, "abc") == good
    assert matching_remote(Client([dict(good, fileName="ab")]), "b", "a", 3, "abc") is None


@pytest.mark.parametrize("item", [
    {"fileName": "a", "action": "hide"},
    {"fileName": "a", "action": "upload", "contentLength": 2, "contentSha1": "abc"},
])
def test_mismatched_or_hidden_remote_fails_closed(item):
    with pytest.raises(RemoteConflict):
        matching_remote(Client([item]), "b", "a", 3, "abc")


def test_unfinished_upload_fails_closed():
    with pytest.raises(RemoteConflict):
        matching_remote(Client(unfinished=[{"fileName": "a"}]), "b", "a", 3, "abc")


def test_unfinished_prefix_sibling_does_not_conflict():
    assert matching_remote(Client(unfinished=[{"fileName": "another-key"}]), "b", "a", 3, "abc") is None


def test_multipart_sha1_metadata_is_accepted():
    item = {"fileName": "a", "action": "upload", "contentLength": 3,
            "contentSha1": "none", "fileInfo": {"large_file_sha1": "abc"}}
    assert matching_remote(Client([item]), "b", "a", 3, "abc") == item


def test_identical_content_on_distinct_records_gets_distinct_keys(tmp_path):
    data = tmp_path / "video.mkv"
    data.write_bytes(b"same content")
    assert object_uuid("path:1:file:4", "fingerprint", data) != object_uuid(
        "path:1:file:5", "fingerprint", data
    )


def test_upload_error_reconciles_once_without_retry(tmp_path):
    import hashlib

    data = tmp_path / "video.mkv"
    data.write_bytes(b"bytes")
    sha1 = hashlib.sha1(b"bytes", usedforsecurity=False).hexdigest()

    class UploadThenVisible(Client):
        uploads = 0

        def upload_file(self, path, bucket, key):
            self.uploads += 1
            self.versions = [{"fileName": key, "action": "upload", "contentLength": 5,
                              "contentSha1": sha1}]
            raise OSError("ambiguous response")

    client = UploadThenVisible()
    result = upload_verified(client, data, "bucket", "key")
    assert result["fileName"] == "key"
    assert client.uploads == 1


def test_upload_conflict_is_not_retried(tmp_path):
    data = tmp_path / "video.mkv"
    data.write_bytes(b"bytes")

    class FailingUpload(Client):
        uploads = 0

        def upload_file(self, path, bucket, key):
            self.uploads += 1
            raise OSError("ambiguous response")

    client = FailingUpload()
    with pytest.raises(RemoteConflict):
        upload_verified(client, data, "bucket", "key")
    assert client.uploads == 1
