"""Standalone native B2 client for MediaMigrate's verified migration uploads."""

import hashlib
import logging
import mimetypes
import time
from pathlib import Path
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

B2_API_VERSION = "v4"
DEFAULT_API_URL = "https://api.backblazeb2.com"
MAX_SINGLE_UPLOAD_SIZE = 5 * 1024 * 1024 * 1024
LARGE_FILE_PART_SIZE = 100 * 1024 * 1024
API_TIMEOUT = 30
UPLOAD_TIMEOUT = 3600


class B2Error(Exception):
    """Raised when B2 returns an unusable response or unavailable bucket."""


class B2Client:
    """Client for B2 authorization, uploads, and verified-object listings."""

    def __init__(self, application_key_id, application_key, api_url=None):
        self._application_key_id = application_key_id
        self._application_key = application_key
        self._api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self._auth = None
        self._auth_at = None

    def _authorize(self):
        if self._auth is not None and time.monotonic() - self._auth_at < 3600:
            return self._auth
        response = requests.get(
            f"{self._api_url}/b2api/{B2_API_VERSION}/b2_authorize_account",
            auth=(self._application_key_id, self._application_key),
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        try:
            storage = data["apiInfo"]["storageApi"]
            api_url = storage["apiUrl"]
            download_url = storage["downloadUrl"]
            token = data["authorizationToken"]
            allowed = storage.get("allowed", {})
            if not isinstance(allowed, dict):
                raise TypeError("invalid allowed buckets")
            allowed_buckets = allowed.get("buckets", [])
            if not isinstance(allowed_buckets, list) or any(
                not isinstance(bucket, dict) for bucket in allowed_buckets
            ):
                raise TypeError("invalid allowed buckets")
            buckets = {
                bucket["name"]: bucket["id"]
                for bucket in allowed_buckets
                if bucket.get("name") and bucket.get("id")
            }
            if not all(isinstance(value, str) and value for value in (api_url, download_url, token)):
                raise ValueError("missing authorization fields")
        except (KeyError, TypeError, ValueError) as exc:
            raise B2Error("malformed B2 authorization response") from exc
        self._auth = {
            "authorizationToken": token,
            "apiUrl": api_url.rstrip("/"),
            "downloadUrl": download_url.rstrip("/"),
            "buckets": buckets,
        }
        self._auth_at = time.monotonic()
        return self._auth

    def _bucket_id(self, bucket_name):
        bucket_id = self._authorize()["buckets"].get(bucket_name)
        if not bucket_id:
            raise B2Error(f"Bucket {bucket_name!r} is not accessible with this application key")
        return bucket_id

    def upload_file(self, local_path, bucket, key, extra_args=None):
        auth = self._authorize()
        bucket_id = self._bucket_id(bucket)
        size = Path(local_path).stat().st_size
        if size > MAX_SINGLE_UPLOAD_SIZE:
            self._upload_large_file(local_path, bucket_id, key, auth)
        else:
            self._upload_single_file(local_path, bucket_id, key, auth)
        logger.info("Uploaded %s to b2://%s/%s", local_path, bucket, key)

    def _upload_single_file(self, local_path, bucket_id, key, auth):
        upload = self._get_upload_url(bucket_id, auth)
        headers = {
            "Authorization": upload["authorizationToken"],
            "X-Bz-File-Name": quote(key, safe="/"),
            "Content-Type": self._content_type(local_path),
            "Content-Length": str(Path(local_path).stat().st_size),
            "X-Bz-Content-Sha1": self._file_sha1(local_path),
        }
        with open(local_path, "rb") as stream:
            response = requests.post(
                upload["uploadUrl"], data=stream, headers=headers, timeout=UPLOAD_TIMEOUT
            )
        response.raise_for_status()

    def _upload_large_file(self, local_path, bucket_id, key, auth):
        headers = {"Authorization": auth["authorizationToken"]}
        response = requests.post(
            f"{auth['apiUrl']}/b2api/{B2_API_VERSION}/b2_start_large_file",
            json={
                "bucketId": bucket_id,
                "fileName": key,
                "contentType": self._content_type(local_path),
                "fileInfo": {"large_file_sha1": self._file_sha1(local_path)},
            },
            headers=headers,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        file_id = _required_string(response.json(), "fileId")
        response = requests.get(
            f"{auth['apiUrl']}/b2api/{B2_API_VERSION}/b2_get_upload_part_url",
            params={"fileId": file_id},
            headers=headers,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        part_upload = response.json()
        try:
            upload_url = part_upload["uploadUrl"]
            upload_token = part_upload["authorizationToken"]
            if not upload_url or not upload_token:
                raise ValueError("missing upload fields")
        except (KeyError, TypeError, ValueError) as exc:
            raise B2Error("malformed B2 part-upload response") from exc

        part_sha1s = []
        with open(local_path, "rb") as stream:
            part_number = 1
            while True:
                part = stream.read(LARGE_FILE_PART_SIZE)
                if not part:
                    break
                digest = hashlib.sha1(part, usedforsecurity=False).hexdigest()
                response = requests.post(
                    upload_url,
                    data=part,
                    headers={
                        "Authorization": upload_token,
                        "X-Bz-Part-Number": str(part_number),
                        "Content-Length": str(len(part)),
                        "X-Bz-Content-Sha1": digest,
                    },
                    timeout=UPLOAD_TIMEOUT,
                )
                response.raise_for_status()
                part_sha1s.append(digest)
                part_number += 1

        response = requests.post(
            f"{auth['apiUrl']}/b2api/{B2_API_VERSION}/b2_finish_large_file",
            json={"fileId": file_id, "partSha1Array": part_sha1s},
            headers=headers,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()

    def list_file_versions(self, bucket, key):
        """Return all file versions whose names start with ``key``."""
        auth = self._authorize()
        bucket_id = self._bucket_id(bucket)
        files = []
        cursor = None
        seen = set()
        while True:
            payload = {"bucketId": bucket_id, "prefix": key, "maxFileCount": 1000}
            if cursor is not None:
                payload.update(startFileName=cursor[0], startFileId=cursor[1])
            data = self._listing_request(auth, "b2_list_file_versions", payload)
            files.extend(_listing_files(data))
            if "nextFileName" not in data or "nextFileId" not in data:
                raise B2Error("malformed B2 file-version pagination cursor")
            name, file_id = data.get("nextFileName"), data.get("nextFileId")
            if name is None and file_id is None:
                return files
            if not isinstance(name, str) or not name or not isinstance(file_id, str) or not file_id:
                raise B2Error("malformed B2 file-version pagination cursor")
            next_cursor = (name, file_id)
            if next_cursor in seen or next_cursor == cursor:
                raise B2Error("B2 file-version listing repeated its pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor

    def list_unfinished_large_files(self, bucket, key):
        """Return all unfinished large uploads whose names start with ``key``."""
        auth = self._authorize()
        bucket_id = self._bucket_id(bucket)
        files = []
        cursor = None
        seen = set()
        while True:
            payload = {"bucketId": bucket_id, "namePrefix": key, "maxFileCount": 1000}
            if cursor is not None:
                payload["startFileId"] = cursor
            data = self._listing_request(auth, "b2_list_unfinished_large_files", payload)
            files.extend(_listing_files(data))
            if "nextFileId" not in data:
                raise B2Error("malformed B2 unfinished-file pagination cursor")
            next_id = data.get("nextFileId")
            if next_id is None:
                return files
            if not isinstance(next_id, str) or not next_id:
                raise B2Error("malformed B2 unfinished-file pagination cursor")
            if next_id in seen or next_id == cursor:
                raise B2Error("B2 unfinished-file listing repeated its pagination cursor")
            seen.add(next_id)
            cursor = next_id

    @staticmethod
    def _listing_request(auth, endpoint, payload):
        response = requests.post(
            f"{auth['apiUrl']}/b2api/{B2_API_VERSION}/{endpoint}",
            json=payload,
            headers={"Authorization": auth["authorizationToken"]},
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise B2Error(f"malformed B2 {endpoint} response")
        return data

    def _get_upload_url(self, bucket_id, auth):
        response = requests.get(
            f"{auth['apiUrl']}/b2api/{B2_API_VERSION}/b2_get_upload_url",
            params={"bucketId": bucket_id},
            headers={"Authorization": auth["authorizationToken"]},
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        try:
            if not data["uploadUrl"] or not data["authorizationToken"]:
                raise ValueError("missing upload fields")
        except (KeyError, TypeError, ValueError) as exc:
            raise B2Error("malformed B2 upload URL response") from exc
        return data

    @staticmethod
    def _content_type(local_path):
        return mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"

    @staticmethod
    def _file_sha1(local_path):
        sha1 = hashlib.sha1(usedforsecurity=False)
        with open(local_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha1.update(chunk)
        return sha1.hexdigest()


def _required_string(data, field):
    try:
        value = data[field]
    except (KeyError, TypeError) as exc:
        raise B2Error(f"malformed B2 response: missing {field}") from exc
    if not isinstance(value, str) or not value:
        raise B2Error(f"malformed B2 response: invalid {field}")
    return value


def _listing_files(data):
    files = data.get("files")
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise B2Error("malformed B2 listing: files must be a list of objects")
    return files
