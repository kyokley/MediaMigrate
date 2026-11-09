from pathlib import Path
from types import SimpleNamespace

import pytest

from mediamigrate.runner import MigrationRunner


class API:
    def __init__(self, candidate, events, *, fail_publish=False):
        self.candidate = candidate
        self.events = events
        self.fail_publish = fail_publish

    def acquire_run(self, run_id):
        self.events.append(("acquire", run_id))

    def candidates(self, limit, bucket, prefix):
        self.events.append(("candidates", limit))
        return [self.candidate]

    def publish(self, pk, body):
        self.events.append(("publish", pk, body))
        if self.fail_publish:
            raise RuntimeError("ambiguous publish")

    def release_run(self, run_id):
        self.events.append(("release", run_id))


def setup_runner(tmp_path, monkeypatch, *, fail_publish=False, mutate_source=False):
    root = tmp_path / "series"
    root.mkdir()
    video = root / "S01E01.mkv"
    video.write_bytes(b"already encoded video")
    (root / "S01E01.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCaption\n")
    candidate = {
        "pk": 8, "path": str(root), "tv": 3, "movie": None, "filename": "",
        "subtitle_files": [], "snapshot": "signed-snapshot", "target": {
            "bucket": "target-bucket", "path": "b2://target-bucket/server-key/",
            "b2_upload_prefix": "server-key/"},
        "children": [{"pk": 12, "filename": video.name, "b2_uuid": None, "size": None,
                      "subtitle_files": []}],
    }
    events = []
    api = API(candidate, events, fail_publish=fail_publish)
    monkeypatch.setattr("mediamigrate.runner.MediaViewerClient", lambda *a, **k: api)
    monkeypatch.setenv("MM_B2_APPLICATION_KEY_ID", "id")
    monkeypatch.setenv("MM_B2_APPLICATION_KEY", "key")
    uploads = []

    class Client:
        def __init__(self, *args):
            pass

    monkeypatch.setattr("mediamigrate.b2.B2Client", Client)

    def upload(client, path, bucket, key):
        uploads.append((bucket, key, Path(path).read_bytes()))
        if mutate_source and Path(path) == video:
            video.write_bytes(b"changed during upload")

    monkeypatch.setattr("mediamigrate.runner.upload_verified", upload)
    args = SimpleNamespace(api_url="https://viewer/api/", apply=True, batch_size=1,
                           bucket=None, prefix=None, writers_paused=True)
    return MigrationRunner(args), events, uploads, video


def test_apply_upload_publish_and_release_order_with_hidden_child(tmp_path, monkeypatch):
    runner, events, uploads, video = setup_runner(tmp_path, monkeypatch)
    summary = runner.run()
    assert (summary.attempted, summary.succeeded, summary.failed) == (1, 1, 0)
    assert [event[0] for event in events] == ["acquire", "candidates", "publish", "release"]
    manifest = events[2][2]
    assert manifest["snapshot"] == "signed-snapshot"
    assert manifest["files"][0]["pk"] == 12
    assert manifest["files"][0]["subtitle_files"]
    assert all(bucket == "target-bucket" for bucket, _, _ in uploads)
    assert all(key.startswith("server-key/") for _, key, _ in uploads)
    assert video.read_bytes() == b"already encoded video"


@pytest.mark.parametrize("fail_publish,mutate_source", [(True, False), (False, True)])
def test_uncertain_publish_or_changed_source_retains_ownership(tmp_path, monkeypatch, capsys,
                                                              fail_publish, mutate_source):
    runner, events, _, _ = setup_runner(tmp_path, monkeypatch, fail_publish=fail_publish,
                                       mutate_source=mutate_source)
    summary = runner.run()
    assert summary.failed == 1
    assert "release" not in [event[0] for event in events]
    assert "ownership retained" in capsys.readouterr().err
    if mutate_source:
        assert "publish" not in [event[0] for event in events]


def test_apply_requires_b2_credentials_before_ownership(tmp_path, monkeypatch):
    runner, events, _, _ = setup_runner(tmp_path, monkeypatch)
    monkeypatch.delenv("MM_B2_APPLICATION_KEY_ID")
    monkeypatch.delenv("MM_B2_APPLICATION_KEY")
    runner.settings.B2_APPLICATION_KEY_ID = None
    runner.settings.B2_APPLICATION_KEY = None
    with pytest.raises(RuntimeError, match="B2 credentials"):
        runner.run()
    assert not events
