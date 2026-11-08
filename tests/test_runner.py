import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def test_snapshot_includes_all_model_fields():
    class Field:
        def __init__(self, name):
            self.attname = name
    class Meta:
        def __init__(self):
            self.concrete_fields = [Field("pk"), Field("date_edited")]
    class Record:
        _meta = Meta()
        pk = 1
        date_edited = "now"
    from mediamigrate.runner import snapshot
    assert snapshot(Record(), []) == ((('pk', '1'), ('date_edited', "'now'")), ())


def test_batch_bound_counts_failed_candidates_in_primary_key_order(monkeypatch):
    class Q:
        def __init__(self, **kwargs):
            pass

        def __or__(self, other):
            return self

    class QuerySet:
        def __init__(self):
            self.records = [SimpleNamespace(pk=1), SimpleNamespace(pk=2), SimpleNamespace(pk=3)]

        def filter(self, *args, **kwargs):
            return self

        def exclude(self, **kwargs):
            return self

        def order_by(self, *args):
            return self

        def __getitem__(self, item):
            return self.records[item]

    django = ModuleType("django")
    django_db = ModuleType("django.db")
    django_models = ModuleType("django.db.models")
    django_models.Q = Q
    monkeypatch.setitem(sys.modules, "django", django)
    monkeypatch.setitem(sys.modules, "django.db", django_db)
    monkeypatch.setitem(sys.modules, "django.db.models", django_models)
    from mediamigrate.runner import Summary

    class MediaPath:
        objects = QuerySet()

    subject = runner(Path("."))
    subject.args.batch_size = 2
    subject.args.apply = False
    subject.summary = Summary()
    subject.settings = None
    attempted = []

    def plan(record):
        attempted.append(record.pk)
        if record.pk == 1:
            raise ValueError("bad record")
        return {"parent": record}

    monkeypatch.setattr(subject, "_plan", plan)
    monkeypatch.setattr(subject, "_execute", lambda plan: pytest.fail("dry-run wrote data"))
    summary = subject._run_candidates(MediaPath)
    assert attempted == [1, 2]
    assert (summary.attempted, summary.succeeded, summary.failed) == (2, 1, 1)
