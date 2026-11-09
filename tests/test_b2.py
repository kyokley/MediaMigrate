import hashlib

import pytest

from mediamigrate import b2


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def authorized():
    return {
        "authorizationToken": "account-token",
        "apiInfo": {
            "storageApi": {
                "apiUrl": "https://storage.example",
                "downloadUrl": "https://download.example",
                "allowed": {"buckets": [{"name": "bucket", "id": "bucket-id"}]},
            }
        },
    }


def client(monkeypatch, get, post):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        if url.endswith("b2_authorize_account"):
            return Response(authorized())
        return get(url, **kwargs)

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return post(url, **kwargs)

    monkeypatch.setattr(b2.requests, "get", fake_get)
    monkeypatch.setattr(b2.requests, "post", fake_post)
    return b2.B2Client("key-id", "secret", api_url="https://api.example"), calls


def test_single_upload_uses_v4_authorization_and_sha1(tmp_path, monkeypatch):
    path = tmp_path / "video file.mkv"
    path.write_bytes(b"video bytes")
    uploaded = {}

    def get(url, **kwargs):
        assert url.endswith("b2_get_upload_url")
        assert kwargs["headers"]["Authorization"] == "account-token"
        return Response({"uploadUrl": "https://upload.example", "authorizationToken": "upload-token"})

    def post(url, **kwargs):
        uploaded.update(url=url, body=kwargs["data"].read(), headers=kwargs["headers"])
        return Response({})

    c, calls = client(monkeypatch, get, post)
    c.upload_file(path, "bucket", "folder/video file.mkv")
    assert uploaded["body"] == b"video bytes"
    assert uploaded["headers"]["Authorization"] == "upload-token"
    assert uploaded["headers"]["X-Bz-File-Name"] == "folder/video%20file.mkv"
    assert uploaded["headers"]["X-Bz-Content-Sha1"] == hashlib.sha1(b"video bytes", usedforsecurity=False).hexdigest()
    auth_call = calls[0]
    assert auth_call[1] == "https://api.example/b2api/v4/b2_authorize_account"
    assert auth_call[2]["auth"] == ("key-id", "secret")


def test_multipart_upload_posts_parts_then_finishes(tmp_path, monkeypatch):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"abcdef")
    monkeypatch.setattr(b2, "MAX_SINGLE_UPLOAD_SIZE", 3)
    monkeypatch.setattr(b2, "LARGE_FILE_PART_SIZE", 3)
    events = []

    def post(url, **kwargs):
        events.append((url, kwargs))
        if url.endswith("b2_start_large_file"):
            return Response({"fileId": "file-id"})
        if url.endswith("b2_finish_large_file"):
            return Response({})
        if url.endswith("/part-upload"):
            return Response({"uploadUrl": "https://upload.example/part", "authorizationToken": "part-token"})
        if url.endswith("/part"):
            return Response({})
        return Response({})

    def get(url, **kwargs):
        assert url.endswith("b2_get_upload_part_url")
        return Response({"uploadUrl": "https://upload.example/part", "authorizationToken": "part-token"})

    c, _ = client(monkeypatch, get, post)
    c.upload_file(path, "bucket", "video.mkv")
    start = next(kwargs for url, kwargs in events if url.endswith("b2_start_large_file"))
    assert start["json"]["bucketId"] == "bucket-id"
    assert start["json"]["fileInfo"]["large_file_sha1"] == hashlib.sha1(
        b"abcdef", usedforsecurity=False
    ).hexdigest()
    uploads = [(url, kwargs) for url, kwargs in events if url == "https://upload.example/part"]
    assert [kwargs["data"] for _, kwargs in uploads] == [b"abc", b"def"]
    digests = [hashlib.sha1(part, usedforsecurity=False).hexdigest() for part in (b"abc", b"def")]
    assert [kwargs["headers"]["X-Bz-Content-Sha1"] for _, kwargs in uploads] == digests
    finish = next(kwargs for url, kwargs in events if url.endswith("b2_finish_large_file"))
    assert finish["json"] == {"fileId": "file-id", "partSha1Array": digests}


def test_multipart_upload_is_verified_from_b2_listing(tmp_path, monkeypatch):
    from mediamigrate.storage import upload_verified

    path = tmp_path / "video.mkv"
    data = b"multipart payload"
    path.write_bytes(data)
    monkeypatch.setattr(b2, "MAX_SINGLE_UPLOAD_SIZE", 3)
    monkeypatch.setattr(b2, "LARGE_FILE_PART_SIZE", 5)
    state = {"file_info": None, "finished": False}

    def get(url, **kwargs):
        assert url.endswith("b2_get_upload_part_url")
        return Response({"uploadUrl": "https://upload.example/part", "authorizationToken": "part-token"})

    def post(url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "b2_list_file_versions":
            files = []
            if state["finished"]:
                files = [{
                    "fileName": "key",
                    "fileId": "file-id",
                    "action": "upload",
                    "contentLength": len(data),
                    "contentSha1": "none",
                    "fileInfo": state["file_info"],
                }]
            return Response({"files": files, "nextFileName": None, "nextFileId": None})
        if endpoint == "b2_list_unfinished_large_files":
            return Response({"files": [], "nextFileId": None})
        if endpoint == "b2_start_large_file":
            state["file_info"] = kwargs["json"]["fileInfo"]
            return Response({"fileId": "file-id"})
        if endpoint == "b2_finish_large_file":
            state["finished"] = True
            return Response({})
        if url == "https://upload.example/part":
            return Response({})
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    c, _ = client(monkeypatch, get, post)
    result = upload_verified(c, path, "bucket", "key")
    assert result["fileInfo"]["large_file_sha1"] == hashlib.sha1(
        data, usedforsecurity=False
    ).hexdigest()
    assert state["finished"]


def test_listing_paginates_versions_and_unfinished_files(monkeypatch):
    responses = {
        "b2_list_file_versions": [
            {"files": [{"fileName": "a"}], "nextFileName": "b", "nextFileId": "2"},
            {"files": [{"fileName": "b"}], "nextFileName": None, "nextFileId": None},
        ],
        "b2_list_unfinished_large_files": [
            {"files": [{"fileName": "c"}], "nextFileId": "next"},
            {"files": [{"fileName": "d"}], "nextFileId": None},
        ],
    }
    seen = []

    def post(url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        seen.append((endpoint, kwargs["json"]))
        return Response(responses[endpoint].pop(0))

    c, _ = client(monkeypatch, lambda *_a, **_k: Response({}), post)
    assert c.list_file_versions("bucket", "a") == [{"fileName": "a"}, {"fileName": "b"}]
    assert c.list_unfinished_large_files("bucket", "a") == [{"fileName": "c"}, {"fileName": "d"}]
    assert seen[1][1]["startFileName"] == "b"
    assert seen[1][1]["startFileId"] == "2"
    assert seen[3][1]["startFileId"] == "next"
    assert all(item[1]["bucketId"] == "bucket-id" for item in seen)


@pytest.mark.parametrize("endpoint,bad", [
    ("b2_list_file_versions", {"nextFileName": None, "nextFileId": None}),
    ("b2_list_unfinished_large_files", {"files": None, "nextFileId": None}),
])
def test_malformed_listing_fails_closed(monkeypatch, endpoint, bad):
    def post(url, **kwargs):
        return Response(bad)

    c, _ = client(monkeypatch, lambda *_a, **_k: Response({}), post)
    method = c.list_file_versions if endpoint.endswith("versions") else c.list_unfinished_large_files
    with pytest.raises(b2.B2Error):
        method("bucket", "key")


def test_repeated_listing_cursor_fails_closed(monkeypatch):
    def post(url, **kwargs):
        return Response({"files": [], "nextFileName": "same", "nextFileId": "same-id"})

    c, _ = client(monkeypatch, lambda *_a, **_k: Response({}), post)
    with pytest.raises(b2.B2Error, match="cursor"):
        c.list_file_versions("bucket", "key")


def test_repeated_unfinished_file_cursor_fails_closed(monkeypatch):
    def post(url, **kwargs):
        return Response({"files": [], "nextFileId": "same-id"})

    c, _ = client(monkeypatch, lambda *_a, **_k: Response({}), post)
    with pytest.raises(b2.B2Error, match="cursor"):
        c.list_unfinished_large_files("bucket", "key")


def test_upload_failure_is_not_retried(tmp_path, monkeypatch):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"payload")
    uploads = 0

    def get(url, **kwargs):
        return Response({"uploadUrl": "https://upload.example", "authorizationToken": "upload-token"})

    def post(url, **kwargs):
        nonlocal uploads
        uploads += 1
        raise OSError("upload response was lost")

    c, _ = client(monkeypatch, get, post)
    with pytest.raises(OSError, match="response was lost"):
        c.upload_file(path, "bucket", "key")
    assert uploads == 1
