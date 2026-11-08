import hashlib
import uuid
from pathlib import Path

KEY_NAMESPACE = uuid.UUID("0dd614c5-d82c-5d3f-84d3-77876c352977")


def hashes(path):
    sha1 = hashlib.sha1(usedforsecurity=False)
    size = 0
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            sha1.update(chunk)
    return size, sha1.hexdigest()


def object_uuid(record_identity, fingerprint, path):
    _, digest = hashes(path)
    source_path = Path(path).resolve()
    return uuid.uuid5(
        KEY_NAMESPACE,
        f"{record_identity}:{fingerprint}:{source_path}:{digest}:{source_path.suffix.lower()}",
    )


class RemoteConflict(Exception):
    pass


def matching_remote(client, bucket, key, size, sha1):
    versions = client.list_file_versions(bucket, key)
    unfinished = client.list_unfinished_large_files(bucket, key)
    if any(not isinstance(item, dict) or not isinstance(item.get("fileName"), str)
           for item in [*versions, *unfinished]):
        raise RemoteConflict("remote listing contains malformed entries")
    exact = [item for item in versions if item.get("fileName") == key]
    if any(item.get("fileName") == key for item in unfinished) or len(exact) > 1:
        raise RemoteConflict("remote key has unfinished or duplicate versions")
    if not exact:
        return None
    item = exact[0]
    if item.get("action") != "upload":
        raise RemoteConflict("remote key has a hide marker or non-upload version")
    content_sha1 = item.get("contentSha1")
    if content_sha1 == "none":
        content_sha1 = (item.get("fileInfo") or {}).get("large_file_sha1")
    if item.get("contentLength") != size or content_sha1 != sha1:
        raise RemoteConflict("remote object does not match prepared bytes")
    return item


def upload_verified(client, path, bucket, key):
    size, sha1 = hashes(path)
    found = matching_remote(client, bucket, key, size, sha1)
    if found:
        return found
    try:
        client.upload_file(path, bucket, key)
    except Exception:  # noqa: BLE001 - upload outcome can be ambiguous for any client failure
        # Reconcile a possibly successful POST once; never blindly retry.
        found = matching_remote(client, bucket, key, size, sha1)
        if found:
            return found
        raise RemoteConflict("upload outcome could not be verified")
    found = matching_remote(client, bucket, key, size, sha1)
    if not found:
        raise RemoteConflict("uploaded object was not confirmed")
    return found
