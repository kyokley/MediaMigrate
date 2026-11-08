import base64
import hashlib
import json
import os
import shutil
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests

from mediamigrate.runner import MigrationRunner

ISOLATED_SETTINGS = "mv_test_settings"
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("DJANGO_SETTINGS_MODULE") != ISOLATED_SETTINGS,
        reason="HTTP integration requires /tmp/opencode isolated PostgreSQL settings",
    ),
    pytest.mark.enable_socket,
]

BUCKET = "mediamigrate-http-test"
BUCKET_ID = "isolated-bucket-id"
ACCOUNT_TOKEN = "isolated-account-token"
UPLOAD_TOKEN = "isolated-upload-token"
DOWNLOAD_TOKEN = "isolated-download-token"
APPLICATION_KEY_ID = "isolated-application-key-id"
APPLICATION_KEY = "isolated-application-key"
PREFIX = "http-integration/"


class LocalB2Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self):
        return self.server.state

    def _json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, body, status=200):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self):
        self._send_json({"error": "unauthorized"}, status=401)

    def _account_authorized(self):
        return self.headers.get("Authorization") == ACCOUNT_TOKEN

    def do_GET(self):
        parsed = urlsplit(self.path)
        self.state["events"].append(("GET", parsed.path))
        if parsed.path == "/b2api/v4/b2_authorize_account":
            expected = "Basic " + base64.b64encode(
                f"{APPLICATION_KEY_ID}:{APPLICATION_KEY}".encode()
            ).decode()
            if self.headers.get("Authorization") != expected:
                return self._unauthorized()
            return self._send_json(
                {
                    "authorizationToken": ACCOUNT_TOKEN,
                    "apiInfo": {
                        "storageApi": {
                            "apiUrl": self.server.base_url,
                            "downloadUrl": self.server.base_url,
                            "allowed": {
                                "buckets": [{"name": BUCKET, "id": BUCKET_ID}]
                            },
                        }
                    },
                }
            )

        if parsed.path == "/b2api/v4/b2_get_upload_url":
            if not self._account_authorized():
                return self._unauthorized()
            if parse_qs(parsed.query).get("bucketId") != [BUCKET_ID]:
                return self._send_json({"error": "unknown bucket"}, status=400)
            return self._send_json(
                {"uploadUrl": f"{self.server.base_url}/upload", "authorizationToken": UPLOAD_TOKEN}
            )

        if parsed.path.startswith("/file/"):
            token = parse_qs(parsed.query).get("Authorization", [None])[0]
            if token != DOWNLOAD_TOKEN and (not token or not token.startswith(f"{DOWNLOAD_TOKEN}-")):
                return self._send_json({"error": "invalid download token"}, status=401)
            pieces = unquote(parsed.path).split("/", 3)
            if len(pieces) != 4 or pieces[2] != BUCKET:
                return self._send_json({"error": "unknown bucket"}, status=404)
            key = pieces[3]
            authorization = self.state["download_authorizations"].get(token)
            if not authorization or not key.startswith(authorization["prefix"]):
                return self._send_json(
                    {"error": "download token prefix mismatch", "key": key, "authorization": authorization},
                    status=401,
                )
            data = self.state["objects"].get(key)
            if data is None:
                return self._send_json({"error": "not found"}, status=404)
            self.state["downloaded"].append(key)
            return self._send_bytes(data)

        return self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        parsed = urlsplit(self.path)
        self.state["events"].append(("POST", parsed.path))
        if parsed.path == "/upload":
            if self.headers.get("Authorization") != UPLOAD_TOKEN:
                return self._unauthorized()
            key = unquote(self.headers.get("X-Bz-File-Name", ""))
            length = int(self.headers.get("Content-Length", "-1"))
            data = self.rfile.read(length)
            digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
            if len(data) != length or self.headers.get("X-Bz-Content-Sha1") != digest:
                return self._send_json({"error": "invalid content"}, status=400)
            with self.state["lock"]:
                self.state["objects"][key] = data
                self.state["versions"].append(
                    {
                        "fileId": f"file-{len(self.state['versions']) + 1}",
                        "fileName": key,
                        "action": "upload",
                        "contentLength": len(data),
                        "contentSha1": digest,
                    }
                )
                self.state["uploads"].append(key)
            return self._send_json(
                {"fileName": key, "fileId": f"file-{len(self.state['versions'])}"}
            )

        if not self._account_authorized():
            return self._unauthorized()
        body = self._json_body()
        if parsed.path == "/b2api/v4/b2_list_file_versions":
            if body.get("bucketId") != BUCKET_ID:
                return self._send_json({"error": "unknown bucket"}, status=400)
            prefix = body.get("prefix", "")
            files = [item for item in self.state["versions"] if item["fileName"].startswith(prefix)]
            return self._send_json({"files": files, "nextFileName": None, "nextFileId": None})

        if parsed.path == "/b2api/v4/b2_list_unfinished_large_files":
            if body.get("bucketId") != BUCKET_ID:
                return self._send_json({"error": "unknown bucket"}, status=400)
            return self._send_json({"files": [], "nextFileId": None})

        if parsed.path == "/b2api/v4/b2_get_download_authorization":
            if body.get("bucketId") != BUCKET_ID or not body.get("fileNamePrefix"):
                return self._send_json({"error": "invalid download authorization"}, status=400)
            token = f"{DOWNLOAD_TOKEN}-{len(self.state['download_authorizations']) + 1}"
            self.state["download_authorizations"][token] = {
                "bucket": BUCKET,
                "prefix": body["fileNamePrefix"],
            }
            return self._send_json({"authorizationToken": token})

        return self._send_json({"error": "not found"}, status=404)

    def log_message(self, *_args):
        pass


@pytest.fixture
def native_b2_server():
    state = {
        "events": [],
        "objects": {},
        "versions": [],
        "uploads": [],
        "downloaded": [],
        "download_authorizations": {},
        "lock": threading.Lock(),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalB2Handler)
    server.daemon_threads = True
    server.state = state
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_fixture(root, files):
    root.mkdir(parents=True)
    sources = {}
    for filename, data in files.items():
        path = root / filename
        path.write_bytes(data)
        sources[path] = data
    return sources


def _runner_args():
    root = Path(os.environ["MV_ROOT"])
    return SimpleNamespace(
        apply=True,
        batch_size=10,
        bucket=BUCKET,
        prefix=PREFIX,
        mediaviewer=root,
        settings=ISOLATED_SETTINGS,
        writers_paused=True,
    )


def _download_exact(url, expected, base_url):
    assert urlsplit(url).netloc == urlsplit(base_url).netloc
    response = requests.get(url, timeout=5)
    assert response.status_code == 200, response.text
    assert response.content == expected
    return hashlib.sha256(response.content).hexdigest()


@pytest.mark.django_db(transaction=True)
def test_native_http_upload_cutover_and_signed_playback(tmp_path, native_b2_server, settings, monkeypatch):
    from django.apps import apps
    from django.conf import settings as django_settings
    from mediaviewer import b2 as b2_module
    from mediaviewer.models import TV, MediaFile, MediaPath, Movie

    monkeypatch.setenv("PATH", "/nonexistent")
    assert shutil.which("ffmpeg") is None
    assert apps.ready
    settings.B2_API_URL = native_b2_server.base_url
    settings.B2_APPLICATION_KEY_ID = APPLICATION_KEY_ID
    settings.B2_APPLICATION_KEY = APPLICATION_KEY
    settings.B2_BUCKET_NAME = BUCKET
    settings.B2_NAME_PREFIX = PREFIX
    b2_module.get_b2_client.cache_clear()
    client = b2_module.get_b2_client()
    assert client._api_url == native_b2_server.base_url

    movie_root = tmp_path / "movie-source"
    movie_name = "Feature Film.mkv"
    movie_sources = _write_fixture(
        movie_root,
        {
            movie_name: b"original already-encoded feature film bytes\x00\x01",
            "Feature Film.en.vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nMovie caption\n",
            "Feature Film.forced.vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nForced caption\n",
        },
    )
    movie = Movie.objects.create(name="Feature Film")
    movie_path = MediaPath.objects.create(
        _path=str(movie_root),
        movie=movie,
        filename=movie_name,
        subtitle_files=["Feature Film.en.vtt"],
    )
    movie_file = MediaFile.objects.create(
        media_path=movie_path,
        filename=movie_name,
        display_name="Feature Film Original Display",
        hide=True,
        subtitle_files=["Feature Film.forced.vtt"],
    )

    tv_root = tmp_path / "tv-source"
    episode_sources = _write_fixture(
        tv_root,
        {
            "Series.S01E01.mkv": b"original episode one encoded bytes",
            "Series.S01E01.en.vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nEpisode one caption\n",
            "Series.S01E02.mkv": b"original hidden episode two encoded bytes",
            "Series.S01E02.en.vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nEpisode two caption\n",
        },
    )
    tv = TV.objects.create(name="Series")
    tv_path = MediaPath.objects.create(_path=str(tv_root), tv=tv)
    episodes = [
        MediaFile.objects.create(
            media_path=tv_path,
            filename="Series.S01E01.mkv",
            display_name="Episode One",
            hide=False,
        ),
        MediaFile.objects.create(
            media_path=tv_path,
            filename="Series.S01E02.mkv",
            display_name="Episode Two Hidden",
            hide=True,
        ),
    ]
    all_sources = {**movie_sources, **episode_sources}
    source_hashes = {str(path): hashlib.sha256(data).hexdigest() for path, data in all_sources.items()}
    original_identity = {
        "movie": (movie.pk, movie_file.pk, movie_file.filename, movie_file.display_name, movie_file.hide),
        "tv": (tv.pk, tuple((episode.pk, episode.filename, episode.display_name, episode.hide) for episode in episodes)),
    }

    result = MigrationRunner(_runner_args()).run()
    assert (result.attempted, result.succeeded, result.failed) == (2, 2, 0)

    movie_path.refresh_from_db()
    tv_path.refresh_from_db()
    movie.refresh_from_db()
    movie_file.refresh_from_db()
    tv.refresh_from_db()
    episodes = list(MediaFile.objects.filter(media_path=tv_path).order_by("pk"))
    assert movie.pk == original_identity["movie"][0]
    assert movie_file.pk == original_identity["movie"][1]
    assert (movie_file.filename, movie_file.display_name, movie_file.hide) == original_identity["movie"][2:]
    assert tv.pk == original_identity["tv"][0]
    assert tuple((item.pk, item.filename, item.display_name, item.hide) for item in episodes) == original_identity["tv"][1]
    assert movie_path.is_b2 and tv_path.is_b2
    assert movie_file.b2_uuid and all(episode.b2_uuid for episode in episodes)
    assert episodes[1].hide is True

    expected_keys = {}
    expected_urls = []
    movie_prefix = movie_path.b2_upload_prefix
    expected_keys[f"{movie_prefix}{movie_file.b2_uuid}{Path(movie_name).suffix}"] = movie_sources[movie_root / movie_name]
    for subtitle_name, original_name in zip(
        movie_file.subtitle_files,
        ("Feature Film.en.vtt", "Feature Film.forced.vtt"),
        strict=True,
    ):
        expected_keys[f"{movie_prefix}{subtitle_name}"] = movie_sources[movie_root / original_name]
    tv_prefix = tv_path.b2_upload_prefix
    for episode, original_name in zip(episodes, ("Series.S01E01.mkv", "Series.S01E02.mkv"), strict=True):
        expected_keys[f"{tv_prefix}{episode.b2_uuid}{Path(episode.filename).suffix}"] = episode_sources[tv_root / original_name]
        caption_name = original_name.removesuffix(".mkv") + ".en.vtt"
        assert len(episode.subtitle_files) == 1
        expected_keys[f"{tv_prefix}{episode.subtitle_files[0]}"] = episode_sources[tv_root / caption_name]

    assert set(native_b2_server.state["objects"]) == set(expected_keys)
    assert set(native_b2_server.state["uploads"]) == set(expected_keys)
    assert all(Path(key).suffix.lower() in {".mkv", ".vtt"} for key in expected_keys)
    for key, data in expected_keys.items():
        assert native_b2_server.state["objects"][key] == data
    for path, data in all_sources.items():
        assert path.read_bytes() == data
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source_hashes[str(path)]

    expected_urls.append((movie.downloadLink(movie.pk), movie_sources[movie_root / movie_name]))
    expected_urls.extend(
        (movie_path.b2_filename_presigned_url(name), movie_sources[movie_root / original_name])
        for name, original_name in zip(
            movie_file.subtitle_files,
            ("Feature Film.en.vtt", "Feature Film.forced.vtt"),
            strict=True,
        )
    )
    for episode, original_name in zip(episodes, ("Series.S01E01.mkv", "Series.S01E02.mkv"), strict=True):
        expected_urls.append((episode.downloadLink(episode.pk), episode_sources[tv_root / original_name]))
        caption_name = original_name.removesuffix(".mkv") + ".en.vtt"
        expected_urls.append(
            (tv_path.b2_filename_presigned_url(episode.subtitle_files[0]), episode_sources[tv_root / caption_name])
        )
    downloaded_hashes = [_download_exact(url, data, native_b2_server.base_url) for url, data in expected_urls]
    assert Counter(native_b2_server.state["downloaded"]) == Counter(expected_keys.keys())
    assert len(downloaded_hashes) == len(expected_keys) == 7

    uploads_before_retry = list(native_b2_server.state["uploads"])
    retry = MigrationRunner(_runner_args()).run()
    assert (retry.attempted, retry.succeeded, retry.failed) == (0, 0, 0)
    assert native_b2_server.state["uploads"] == uploads_before_retry
    assert Counter(path for method, path in native_b2_server.state["events"] if method == "POST")[
        "/upload"
    ] == 7
    endpoints = {path for _, path in native_b2_server.state["events"]}
    assert {
        "/b2api/v4/b2_authorize_account",
        "/b2api/v4/b2_get_upload_url",
        "/upload",
        "/b2api/v4/b2_list_file_versions",
        "/b2api/v4/b2_list_unfinished_large_files",
        "/b2api/v4/b2_get_download_authorization",
    } <= endpoints
    assert sum(path.startswith("/file/") for _, path in native_b2_server.state["events"]) == 7
    print(
        "NATIVE_B2_EVIDENCE "
        + json.dumps(
            {
                "migrated_paths": result.succeeded,
                "uploaded_objects": len(native_b2_server.state["uploads"]),
                "source_files": len(all_sources),
                "signed_gets": len(native_b2_server.state["downloaded"]),
                "retry_attempted": retry.attempted,
                "sha256": sorted(source_hashes.values()),
                "download_sha256": sorted(downloaded_hashes),
            },
            sort_keys=True,
        )
    )
    assert django_settings.B2_API_URL == native_b2_server.base_url
    b2_module.get_b2_client.cache_clear()
