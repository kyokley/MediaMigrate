import copy
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .storage import object_uuid, upload_verified

VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".webm", ".mpeg",
    ".mpg", ".mpe", ".mpv", ".m2v", ".ts", ".m2ts", ".mts", ".3gp",
    ".flv", ".ogv", ".vob", ".divx", ".f4v", ".mxf",
}
LOCK_NAMESPACE = 0x4D4D
LOCK_KEY = 0x20250308


@dataclass
class Summary:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0


class MigrationRunner:
    def __init__(self, args):
        self.args = args
        self.summary = Summary()
        self._setup_django()

    def _setup_django(self):
        viewer = Path(self.args.mediaviewer).resolve()
        if not viewer.is_dir():
            raise RuntimeError("MediaViewer checkout does not exist")
        sys.path.insert(0, str(viewer))
        os.environ["DJANGO_SETTINGS_MODULE"] = self.args.settings
        import django
        django.setup()
        from django.conf import settings
        if self.args.bucket:
            settings.B2_BUCKET_NAME = self.args.bucket
        if self.args.prefix is not None:
            settings.B2_NAME_PREFIX = self.args.prefix
        self.settings = settings

    def run(self):
        from django.db import connection
        from mediaviewer.models import MediaPath
        if self.args.apply:
            if connection.vendor != "postgresql":
                raise RuntimeError("apply requires PostgreSQL")
            if not (getattr(self.settings, "B2_APPLICATION_KEY_ID", None) and getattr(self.settings, "B2_APPLICATION_KEY", None)):
                raise RuntimeError("B2 credentials are required for apply")
            if not (self.args.bucket or getattr(self.settings, "B2_BUCKET_NAME", None)):
                raise RuntimeError("a B2 bucket is required for apply")
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NAMESPACE, LOCK_KEY])
                acquired = cursor.fetchone()[0]
            if not acquired:
                raise RuntimeError("another MediaMigrate run holds the advisory lock")
            try:
                return self._run_candidates(MediaPath)
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, LOCK_KEY])
        return self._run_candidates(MediaPath)

    def _run_candidates(self, MediaPath):
        from django.db.models import Q
        qs = (MediaPath.objects.filter(skip=False).exclude(_path__startswith="b2://")
              .exclude(_path="").filter(Q(tv__isnull=False) | Q(movie__isnull=False)).order_by("pk"))
        candidates = list(qs[: self.args.batch_size])
        for parent in candidates:
            try:
                self.summary.attempted += 1
                plan = self._plan(parent)
                if self.args.apply:
                    self._execute(plan)
                    self.summary.succeeded += 1
                else:
                    self.summary.succeeded += 1
                    print(f"would migrate MediaPath {parent.pk}")
            except Exception as exc:  # noqa: BLE001 - record failures must not stop the bounded batch
                self.summary.failed += 1
                print(f"MediaPath {parent.pk}: {exc.__class__.__name__}: {safe_message(exc, self.settings)}", file=sys.stderr)
        return self.summary

    def _plan(self, parent):
        from mediaviewer.models import MediaFile
        if bool(parent.tv_id) == bool(parent.movie_id):
            raise ValueError("path must reference exactly one TV or movie record")
        if parent.filename and parent.tv_id:
            raise ValueError("TV path cannot have a movie filename")
        root = Path(parent._path)
        if not root.exists() or not root.is_dir():
            raise ValueError("media directory is missing")
        root = root.resolve(strict=True)
        children = list(MediaFile.objects.filter(media_path_id=parent.pk).order_by("pk"))
        if any(getattr(child, "b2_uuid", None) for child in children):
            raise ValueError("legacy path has partial B2 metadata")
        if parent.tv_id and not children:
            raise ValueError("TV path has no episodes")
        selected = []
        if parent.tv_id:
            for child in children:
                selected.append((child, self._contained(root, child.filename)))
        else:
            if not parent.movie:
                raise ValueError("movie reference is missing")
            if parent.filename:
                movie_path = self._contained(root, parent.filename)
                if movie_path.suffix.lower() not in VIDEO_SUFFIXES:
                    raise ValueError("movie filename is not a recognized video")
            else:
                candidates = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES]
                if not candidates:
                    raise ValueError("no direct-child movie video found")
                candidates.sort(key=lambda p: (-p.stat().st_size, p.name))
                if len(candidates) > 1 and candidates[0].stat().st_size == candidates[1].stat().st_size:
                    raise ValueError("largest movie candidates tie")
                movie_path = self._contained(root, candidates[0].name)
            matches = [child for child in children if child.filename == movie_path.name]
            if len(matches) > 1 or any(child.filename != movie_path.name for child in children):
                raise ValueError("movie MediaFile records would be duplicated or stranded")
            selected.append((matches[0] if matches else None, movie_path))
        if any(path.stat().st_size == 0 for _, path in selected):
            raise ValueError("empty video source")
        if any(path.suffix.lower() not in VIDEO_SUFFIXES for _, path in selected):
            raise ValueError("video source has an unsupported filename extension")
        plans = []
        for child, video in selected:
            parent_subtitles = parent.subtitle_files
            if parent.tv_id and parent_subtitles not in (None, []):
                raise ValueError("TV parent subtitle metadata is not supported")
            child_subtitles = child.subtitle_files if child else None
            for metadata in (parent_subtitles, child_subtitles):
                if metadata is not None and not isinstance(metadata, list):
                    raise ValueError("subtitle metadata must be a list")
            explicit_subtitles = [*(parent_subtitles or []), *(child_subtitles or [])]
            subtitles = self._subtitles(root, video, explicit_subtitles)
            from mediamigrate.media import inspect_video, source_fingerprint
            inspect_video(video, subtitles)
            fingerprint = source_fingerprint(video, subtitles)
            plans.append({"child": child, "video": video, "subtitles": subtitles, "fingerprint": fingerprint})
        return {"parent": parent, "children": children, "plans": plans, "root": root,
                "snapshot": snapshot(parent, children)}

    @staticmethod
    def _contained(root, filename):
        if not filename or Path(filename).name != filename or filename in {".", ".."}:
            raise ValueError("unsafe media filename")
        candidate = root / filename
        if candidate.is_symlink():
            raise ValueError("symbolic-link media files are not supported")
        path = candidate.resolve(strict=True)
        if root not in path.parents or not path.is_file():
            raise ValueError("media file escapes its media directory")
        return path

    def _subtitles(self, root, video, explicit):
        if explicit is not None and not isinstance(explicit, list):
            raise ValueError("subtitle metadata must be a list")
        names = []
        for item in explicit or []:
            if not isinstance(item, str) or Path(item).suffix.lower() != ".vtt":
                raise ValueError("unsupported explicit subtitle format")
            names.append(item)
        stems = {video.stem, video.name}
        for candidate in root.iterdir():
            if not candidate.is_file():
                continue
            base = candidate.name[: -len(candidate.suffix)]
            matched = any(
                base == stem or base.startswith((stem + ".", stem + "-"))
                for stem in stems
            )
            if not matched:
                continue
            if candidate.suffix.lower() != ".vtt":
                if candidate.suffix.lower() in {
                    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"
                }:
                    raise ValueError("matched subtitle sidecar must be WebVTT")
                continue
            names.append(candidate.name)
        names = sorted(set(names))
        result = []
        for name in dict.fromkeys(names):
            result.append(self._contained(root, name))
        return result

    def _execute(self, plan):
        from django.db import transaction
        from mediaviewer.b2 import get_b2_client
        from mediaviewer.models import MediaFile, MediaPath

        from mediamigrate.media import prepare_video
        parent = plan["parent"]
        prepared = []
        for item in plan["plans"]:
            result = prepare_video(item["video"], item["subtitles"])
            if (
                Path(result.video).resolve() != item["video"]
                or tuple(Path(p).resolve() for p in result.subtitles)
                != tuple(item["subtitles"])
                or result.fingerprint != item["fingerprint"]
            ):
                raise RuntimeError("preparation must preserve original media files")
            prepared.append((item, result))
        # Compute planned storage prefix through MediaViewer's own sharding logic.
        staged_parent = copy.copy(parent)
        prefix = self.args.prefix if self.args.prefix is not None else getattr(self.settings, "B2_NAME_PREFIX", "")
        local_root = plan["root"].name
        bucket = self.args.bucket or self.settings.B2_BUCKET_NAME
        logical_key_prefix = f"{prefix.rstrip('/') + '/' if prefix else ''}{parent.pk}/{local_root}/"
        staged_parent._path = f"b2://{bucket}/{logical_key_prefix}"
        object_prefix = staged_parent.b2_upload_prefix
        client = get_b2_client()
        updates = []
        verify_sources(plan)
        for item, output in prepared:
            child = item["child"]
            identity = f"{parent.pk}:mediafile:{child.pk}" if child else f"{parent.pk}:movie:{parent.movie_id}"
            uid = object_uuid(identity, item["fingerprint"], output.video)
            video_key = f"{object_prefix}{uid}{item['video'].suffix}"
            upload_verified(client, output.video, bucket, video_key)
            subtitle_ids = []
            for subtitle in output.subtitles:
                sid = object_uuid(identity, item["fingerprint"], subtitle)
                upload_verified(client, subtitle, bucket, f"{object_prefix}{sid}.vtt")
                subtitle_ids.append(f"{sid}.vtt")
            updates.append((child, uid, Path(output.video).stat().st_size, subtitle_ids))
        verify_sources(plan)
        with transaction.atomic():
            locked = MediaPath.objects.select_for_update().get(pk=parent.pk)
            current = list(MediaFile.objects.select_for_update().filter(media_path_id=parent.pk).order_by("pk"))
            if snapshot(locked, current) != plan["snapshot"]:
                raise RuntimeError("database snapshot changed before publication")
            # Recheck all source input fingerprints at publication boundary.
            verify_sources(plan)
            locked._path = f"b2://{bucket}/{logical_key_prefix}"
            if locked.movie_id:
                locked.filename = plan["plans"][0]["video"].name
                locked.subtitle_files = updates[0][3]
            locked.save(update_fields=["_path", "filename", "subtitle_files"])
            for child, uid, size, subtitles in updates:
                if child is None:
                    child = MediaFile(media_path=locked, filename=plan["plans"][0]["video"].name)
                    child.b2_uuid = uid
                    child.size = size
                    child.subtitle_files = subtitles
                    child.save()
                    continue
                child.b2_uuid = uid
                child.size = size
                child.subtitle_files = subtitles
                child.save(update_fields=["b2_uuid", "size", "subtitle_files"])


def snapshot(parent, children):
    def values(obj):
        return tuple((field.attname, repr(getattr(obj, field.attname))) for field in obj._meta.concrete_fields)
    return values(parent), tuple(values(child) for child in children)


def verify_sources(plan):
    from mediamigrate.media import source_fingerprint
    for item in plan["plans"]:
        if source_fingerprint(item["video"], item["subtitles"]) != item["fingerprint"]:
            raise RuntimeError("source files changed during migration")


def safe_message(error, settings=None):
    message = str(error).split("?", 1)[0]
    message = re.sub(r"https?://\S+", "[URL]", message)
    for name in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY"):
        secret = getattr(settings, name, None) if settings is not None else None
        if secret:
            message = message.replace(str(secret), "[REDACTED]")
    return message[:240] or error.__class__.__name__
