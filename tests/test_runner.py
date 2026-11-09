from pathlib import Path
from types import SimpleNamespace

import pytest

from mediamigrate.runner import MigrationRunner


def runner(tmp_path):
    obj = object.__new__(MigrationRunner)
    obj.args = SimpleNamespace()
    return obj


def test_containment_rejects_traversal_and_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    (root / "link.mp4").symlink_to(outside)
    with pytest.raises(ValueError):
        runner(tmp_path)._contained(root, "../outside.mp4")
    with pytest.raises(ValueError):
        runner(tmp_path)._contained(root, "link.mp4")


def test_containment_rejects_in_tree_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "real.mkv"
    target.write_bytes(b"video")
    (root / "alias.mp4").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic-link"):
        runner(tmp_path)._contained(root, "alias.mp4")


def test_subtitle_collection_does_not_cross_episode_stems(tmp_path):
    root = tmp_path / "show"
    root.mkdir()
    video = root / "Show.S01E01.mkv"
    video.touch()
    (root / "Show.S01E01.en.vtt").write_text("WEBVTT\n")
    (root / "Show.S01E02.en.vtt").write_text("WEBVTT\n")
    got = runner(tmp_path)._subtitles(root, video, [])
    assert [item.name for item in got] == ["Show.S01E01.en.vtt"]


def test_explicit_non_webvtt_caption_is_rejected(tmp_path):
    root = tmp_path / "show"
    root.mkdir()
    video = root / "Show.S01E01.mkv"
    video.touch()
    (root / "captions.srt").touch()
    with pytest.raises(ValueError, match="unsupported explicit subtitle"):
        runner(tmp_path)._subtitles(root, video, ["captions.srt"])


@pytest.mark.parametrize("suffix", [".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"])
def test_matched_unsupported_caption_sidecar_is_rejected(tmp_path, suffix):
    root = tmp_path / "show"
    root.mkdir()
    video = root / "Show.S01E01.mkv"
    video.touch()
    (root / f"Show.S01E01.en{suffix}").touch()
    with pytest.raises(ValueError, match="must be WebVTT"):
        runner(tmp_path)._subtitles(root, video, [])


def test_unrelated_unsupported_caption_is_ignored_and_vtt_order_is_stable(tmp_path):
    root = tmp_path / "show"
    root.mkdir()
    video = root / "Show.S01E01.mkv"
    video.touch()
    (root / "Show.S01E01.z.vtt").write_text("WEBVTT\n")
    (root / "Show.S01E01.a.vtt").write_text("WEBVTT\n")
    (root / "Show.S01E02.a.srt").touch()
    got = runner(tmp_path)._subtitles(root, video, [])
    assert [item.name for item in got] == ["Show.S01E01.a.vtt", "Show.S01E01.z.vtt"]


def test_malformed_subtitle_metadata_is_rejected(tmp_path):
    root = tmp_path / "show"
    root.mkdir()
    video = root / "Show.S01E01.mkv"
    video.touch()
    with pytest.raises(ValueError, match="must be a list"):
        runner(tmp_path)._subtitles(root, video, {"captions": "x.vtt"})


def test_batch_counts_invalid_candidates_and_continues(capsys):
    subject = runner(Path("."))
    from mediamigrate.runner import Summary
    subject.summary = Summary()
    subject.settings = None
    subject.args = SimpleNamespace(apply=False)
    candidates = [{"pk": 1, "error": "bad record"}, {"pk": 2, "error": "bad record"}]
    summary = subject._run_candidates(candidates)
    assert (summary.attempted, summary.succeeded, summary.failed) == (2, 0, 2)
    assert "MediaPath 1" in capsys.readouterr().err


@pytest.mark.parametrize("filename", [None, ""])
def test_candidate_parser_keeps_all_api_inventory_children(filename):
    from mediamigrate.runner import MigrationRunner
    parent = MigrationRunner._record({
        "pk": 9, "path": "/media/show", "tv": 4, "movie": None, "filename": filename,
        "subtitle_files": [], "snapshot": "opaque", "target": {
            "bucket": "bucket", "path": "b2://bucket/key/", "b2_upload_prefix": "key/"},
        "children": [{"pk": 11, "filename": "hidden.mkv", "b2_uuid": None,
                      "size": 5, "subtitle_files": []}],
    })
    assert parent.children[0].pk == 11
    assert parent.children[0].filename == "hidden.mkv"


def test_dry_run_needs_no_b2_credentials_or_write_endpoints(monkeypatch, capsys):
    from mediamigrate.runner import MigrationRunner
    events = []

    class API:
        def __init__(self, *args, **kwargs):
            pass

        def candidates(self, *args):
            events.append("inventory")
            return []

        def acquire_run(self, *_):
            pytest.fail("dry run acquired ownership")

    monkeypatch.setattr("mediamigrate.runner.MediaViewerClient", API)
    monkeypatch.delenv("MM_B2_APPLICATION_KEY_ID", raising=False)
    monkeypatch.delenv("MM_B2_APPLICATION_KEY", raising=False)
    subject = MigrationRunner(SimpleNamespace(api_url="https://viewer/api/", apply=False,
                                              batch_size=2, bucket=None, prefix=None))
    summary = subject.run()
    assert (summary.attempted, summary.succeeded, summary.failed) == (0, 0, 0)
    assert events == ["inventory"]
    assert capsys.readouterr().out == ""


def test_programmatic_batch_over_limit_fails_before_ownership(monkeypatch):
    from mediamigrate.runner import MigrationRunner
    events = []

    class API:
        MAX_BATCH = 1000

        def __init__(self, *args, **kwargs):
            pass

        def acquire_run(self, *_):
            events.append("acquire")

    monkeypatch.setattr("mediamigrate.runner.MediaViewerClient", API)
    monkeypatch.setenv("MM_B2_APPLICATION_KEY_ID", "id")
    monkeypatch.setenv("MM_B2_APPLICATION_KEY", "key")
    subject = MigrationRunner(SimpleNamespace(api_url="https://viewer/api/", apply=True,
                                              writers_paused=True, batch_size=1001,
                                              bucket=None, prefix=None))
    with pytest.raises(ValueError, match="between 1 and 1000"):
        subject.run()
    assert events == []


@pytest.mark.parametrize("change", [
    {"pk": True}, {"pk": "1"}, {"tv": 0}, {"movie": "2"}, {"snapshot": ""},
    {"filename": 123},
])
def test_candidate_parser_rejects_malformed_identifiers_and_fields(change):
    from mediamigrate.runner import MigrationRunner
    raw = {
        "pk": 1, "path": "/media", "tv": 2, "movie": None, "filename": "",
        "subtitle_files": [], "snapshot": "opaque", "target": {
            "bucket": "bucket", "path": "b2://bucket/key/", "b2_upload_prefix": "key/"},
        "children": [],
    }
    raw.update(change)
    with pytest.raises(ValueError):
        MigrationRunner._record(raw)


def test_candidate_parser_rejects_duplicate_child_ids():
    from mediamigrate.runner import MigrationRunner
    child = {"pk": 4, "filename": "e.mkv", "b2_uuid": None, "size": None, "subtitle_files": []}
    raw = {
        "pk": 1, "path": "/media", "tv": 2, "movie": None, "filename": "",
        "subtitle_files": [], "snapshot": "opaque", "target": {
            "bucket": "bucket", "path": "b2://bucket/key/", "b2_upload_prefix": "key/"},
        "children": [child, dict(child)],
    }
    with pytest.raises(ValueError, match="duplicated"):
        MigrationRunner._record(raw)
