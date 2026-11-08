import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediamigrate.runner import LOCK_KEY, LOCK_NAMESPACE, MigrationRunner

ISOLATED_SETTINGS = "mv_test_settings"
pytestmark = pytest.mark.skipif(
    os.environ.get("DJANGO_SETTINGS_MODULE") != ISOLATED_SETTINGS,
    reason="integration tests require /tmp/opencode isolated PostgreSQL settings",
)


class LocalB2:
    """Controlled B2 boundary that enforces exact-key upload verification."""

    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.fail_before_store = False
        self.fail_on_upload_number = None
        self.after_upload = None

    def list_file_versions(self, bucket, key):
        return [dict(value) for name, value in self.objects.items() if name.startswith(key)]

    def list_unfinished_large_files(self, bucket, key):
        return []

    def upload_file(self, local_path, bucket, key):
        self.uploads.append(key)
        if self.fail_before_store or len(self.uploads) == self.fail_on_upload_number:
            raise OSError("injected upload failure")
        data = Path(local_path).read_bytes()
        self.objects[key] = {
            "fileName": key,
            "action": "upload",
            "contentLength": len(data),
            "contentSha1": hashlib.sha1(data, usedforsecurity=False).hexdigest(),
        }
        if self.after_upload:
            self.after_upload()


@pytest.fixture
def django_models():
    from mediaviewer.models import TV, Comment, MediaFile, MediaPath, Movie

    return SimpleNamespace(
        Comment=Comment, MediaFile=MediaFile, MediaPath=MediaPath, Movie=Movie, TV=TV
    )


@pytest.fixture
def b2(monkeypatch, settings):
    settings.B2_APPLICATION_KEY_ID = "isolated-test-id"
    settings.B2_APPLICATION_KEY = "isolated-test-key"
    settings.B2_BUCKET_NAME = "isolated-test-bucket"
    settings.B2_NAME_PREFIX = "integration/"
    client = LocalB2()
    from mediaviewer import b2 as b2_module

    monkeypatch.setattr(b2_module, "get_b2_client", lambda: client)
    return client


def runner_args(apply, batch_size=10):
    root = Path(os.environ["MV_ROOT"])
    return SimpleNamespace(
        apply=apply,
        batch_size=batch_size,
        bucket="isolated-test-bucket",
        prefix="integration/",
        mediaviewer=root,
        settings=ISOLATED_SETTINGS,
        writers_paused=True,
    )


def add_movie(models, root, *, filename="Film.mkv", parent_subtitles=None, child_subtitles=None, child=True):
    root.mkdir(parents=True)
    (root / filename).write_bytes(b"already encoded movie bytes")
    for name in set(parent_subtitles or []) | set(child_subtitles or []):
        (root / name).write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCaption\n")
    movie = models.Movie.objects.create(name=root.name)
    path = models.MediaPath.objects.create(
        _path=str(root), movie=movie, subtitle_files=parent_subtitles or []
    )
    media_file = None
    if child:
        media_file = models.MediaFile.objects.create(
            media_path=path,
            filename=filename,
            display_name="Original display name",
            hide=True,
            subtitle_files=child_subtitles or [],
        )
    return path, movie, media_file


def add_tv(models, root, filenames):
    root.mkdir(parents=True)
    tv = models.TV.objects.create(name=root.name)
    path = models.MediaPath.objects.create(_path=str(root), tv=tv)
    children = []
    for index, filename in enumerate(filenames):
        (root / filename).write_bytes(f"encoded episode {index}".encode())
        children.append(
            models.MediaFile.objects.create(
                media_path=path,
                filename=filename,
                display_name=f"Episode {index}",
                hide=index == 1,
            )
        )
    return path, tv, children


@pytest.mark.django_db(transaction=True)
def test_real_cli_dry_run_is_bounded_ordered_and_read_only(
    tmp_path, django_models, monkeypatch, capsys
):
    missing_movie = django_models.Movie.objects.create(name="missing")
    missing = django_models.MediaPath.objects.create(
        _path=str(tmp_path / "missing"), movie=missing_movie
    )
    valid_path, _, _ = add_movie(django_models, tmp_path / "valid")
    later_path, _, _ = add_movie(django_models, tmp_path / "later")
    source_file = tmp_path / "valid" / "Film.mkv"
    source_before = (source_file.read_bytes(), source_file.stat().st_mtime_ns)
    valid_edited = valid_path.date_edited

    from mediaviewer import b2 as b2_module

    monkeypatch.setattr(
        b2_module,
        "get_b2_client",
        lambda: pytest.fail("dry-run called B2"),
    )
    from mediamigrate.cli import main

    exit_code = main(
        [
            "2",
            "--mediaviewer",
            os.environ["MV_ROOT"],
            "--settings",
            ISOLATED_SETTINGS,
            "--bucket",
            "isolated-test-bucket",
        ]
    )
    output = capsys.readouterr()
    assert exit_code == 1
    assert f"MediaPath {missing.pk}" in output.err
    assert f"would migrate MediaPath {valid_path.pk}" in output.out
    assert f"MediaPath {later_path.pk}" not in output.out
    assert (missing._path, valid_path._path, later_path._path) == (
        str(tmp_path / "missing"),
        str(tmp_path / "valid"),
        str(tmp_path / "later"),
    )
    assert django_models.MediaFile.objects.filter(media_path=valid_path, b2_uuid__isnull=False).count() == 0
    valid_path.refresh_from_db()
    assert valid_path.date_edited == valid_edited
    assert (source_file.read_bytes(), source_file.stat().st_mtime_ns) == source_before


@pytest.mark.django_db(transaction=True)
def test_hidden_tv_episode_is_published_with_same_identity(tmp_path, django_models, b2):
    path, _, children = add_tv(django_models, tmp_path / "series", ["S01E01.mkv", "S01E02.mkv"])
    before = [(child.pk, child.filename, child.hide, child.date_edited) for child in children]

    summary = MigrationRunner(runner_args(True)).run()

    assert (summary.attempted, summary.succeeded, summary.failed) == (1, 1, 0)
    path.refresh_from_db()
    after = list(django_models.MediaFile.objects.filter(media_path=path).order_by("pk"))
    assert path._path.startswith("b2://isolated-test-bucket/integration/")
    assert [(item.pk, item.filename, item.hide, item.date_edited) for item in after] == before
    assert all(item.b2_uuid for item in after)
    assert len(b2.uploads) == 2


@pytest.mark.parametrize(
    "parent_subtitles",
    [["S01E01.en.vtt"], {"unexpected": "S01E01.en.vtt"}],
)
@pytest.mark.django_db(transaction=True)
def test_tv_parent_subtitle_metadata_fails_before_upload_or_cutover(
    tmp_path, django_models, b2, parent_subtitles
):
    path, _, children = add_tv(
        django_models, tmp_path / "tv-parent-captions", ["S01E01.mkv"]
    )
    caption = tmp_path / "tv-parent-captions" / "S01E01.en.vtt"
    caption.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCaption\n")
    path.subtitle_files = parent_subtitles
    path.save(update_fields=["subtitle_files"])
    original_video = (tmp_path / "tv-parent-captions" / "S01E01.mkv").read_bytes()

    summary = MigrationRunner(runner_args(True)).run()

    path.refresh_from_db()
    children[0].refresh_from_db()
    assert summary.attempted == summary.failed == 1
    assert path._path == str(tmp_path / "tv-parent-captions")
    assert path.subtitle_files == parent_subtitles
    assert children[0].b2_uuid is None
    assert not b2.uploads
    assert (tmp_path / "tv-parent-captions" / "S01E01.mkv").read_bytes() == original_video
    assert caption.read_text().startswith("WEBVTT")


@pytest.mark.django_db(transaction=True)
def test_movie_keeps_existing_mediafile_and_unions_parent_child_captions(
    tmp_path, django_models, b2
):
    path, _, child = add_movie(
        django_models,
        tmp_path / "film",
        parent_subtitles=["Film.en.vtt"],
        child_subtitles=["Film.forced.vtt"],
    )
    user = __import__("django.contrib.auth", fromlist=["get_user_model"]).get_user_model().objects.create_user(
        username="migrate-test"
    )
    comment = django_models.Comment.objects.create(user=user, media_file=child, viewed=True)
    child_id, original_name, original_edited, original_hide = (
        child.pk,
        child.filename,
        child.date_edited,
        child.hide,
    )

    summary = MigrationRunner(runner_args(True)).run()

    assert summary.succeeded == 1 and summary.failed == 0
    path.refresh_from_db()
    child.refresh_from_db()
    assert child.pk == child_id
    assert child.filename == original_name
    assert child.date_edited == original_edited
    assert child.hide == original_hide
    assert child.comments.get(pk=comment.pk).viewed
    assert len(path.subtitle_files) == 2
    assert set(child.subtitle_files) == set(path.subtitle_files)
    assert len(b2.uploads) == 3


@pytest.mark.django_db(transaction=True)
def test_membership_change_aborts_publish_then_retry_reuses_remote_objects(
    tmp_path, django_models, b2
):
    path, _, _ = add_tv(django_models, tmp_path / "snapshot", ["E01.mkv"])
    changed = False

    def mutate_once():
        nonlocal changed
        if not changed:
            changed = True
            (tmp_path / "snapshot" / "E02.mkv").write_bytes(b"episode inserted during upload")
            django_models.MediaFile.objects.create(
                media_path=path,
                filename="E02.mkv",
                display_name="Episode added during upload",
            )

    b2.after_upload = mutate_once
    first = MigrationRunner(runner_args(True)).run()
    path.refresh_from_db()
    assert first.failed == 1 and first.succeeded == 0
    assert not path.is_b2
    assert all(item.b2_uuid is None for item in django_models.MediaFile.objects.filter(media_path=path))
    upload_count = len(b2.uploads)

    b2.after_upload = None
    second = MigrationRunner(runner_args(True)).run()
    path.refresh_from_db()
    assert second.succeeded == 1 and second.failed == 0
    assert path.is_b2
    assert len(b2.uploads) == upload_count + 1
    assert b2.uploads.count(b2.uploads[0]) == 1


@pytest.mark.django_db(transaction=True)
def test_partial_database_publish_rolls_back_then_reuses_uploads(
    tmp_path, django_models, b2, monkeypatch
):
    path, _, children = add_tv(django_models, tmp_path / "rollback", ["E01.mkv", "E02.mkv"])
    original_save = django_models.MediaFile.save

    def fail_second(self, *args, **kwargs):
        if self.pk == children[1].pk and self.b2_uuid:
            raise RuntimeError("injected database publication failure")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(django_models.MediaFile, "save", fail_second)
    failed = MigrationRunner(runner_args(True)).run()
    assert failed.failed == 1
    path.refresh_from_db()
    assert not path.is_b2
    assert list(django_models.MediaFile.objects.filter(media_path=path, b2_uuid__isnull=True).values_list("pk", flat=True)) == [
        children[0].pk,
        children[1].pk,
    ]
    upload_count = len(b2.uploads)

    monkeypatch.setattr(django_models.MediaFile, "save", original_save)
    succeeded = MigrationRunner(runner_args(True)).run()
    assert succeeded.succeeded == 1
    assert len(b2.uploads) == upload_count


@pytest.mark.django_db(transaction=True)
def test_upload_failure_counts_path_and_retry_succeeds(tmp_path, django_models, b2):
    path, _, _ = add_movie(
        django_models,
        tmp_path / "upload-failure",
        parent_subtitles=["Film.en.vtt"],
    )
    b2.fail_on_upload_number = 2
    first = MigrationRunner(runner_args(True)).run()
    path.refresh_from_db()
    assert first.attempted == first.failed == 1
    assert not path.is_b2
    video_key = b2.uploads[0]
    assert video_key in b2.objects

    b2.fail_on_upload_number = None
    upload_count = len(b2.uploads)
    second = MigrationRunner(runner_args(True)).run()
    path.refresh_from_db()
    assert second.succeeded == 1 and path.is_b2
    assert b2.uploads.count(video_key) == 1
    assert len(b2.uploads) == upload_count + 1


@pytest.mark.django_db(transaction=True)
def test_postgres_advisory_lock_rejects_other_run_and_releases_afterward(
    tmp_path, django_models, b2
):
    from django.db import connections

    add_movie(django_models, tmp_path / "lock")
    other = connections["default"].copy(alias="mediamigrate-lock-check")
    other.ensure_connection()
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s, %s)", [LOCK_NAMESPACE, LOCK_KEY])
        with pytest.raises(RuntimeError, match="another MediaMigrate run"):
            MigrationRunner(runner_args(True)).run()
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, LOCK_KEY])

        result = MigrationRunner(runner_args(True)).run()
        assert result.succeeded == 1
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NAMESPACE, LOCK_KEY])
            assert cursor.fetchone()[0]
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, LOCK_KEY])
    finally:
        other.close()
