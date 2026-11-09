import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from .client import MediaViewerClient
from .storage import object_uuid, upload_verified

VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".webm", ".mpeg",
    ".mpg", ".mpe", ".mpv", ".m2v", ".ts", ".m2ts", ".mts", ".3gp",
    ".flv", ".ogv", ".vob", ".divx", ".f4v", ".mxf",
}
MAX_BATCH_SIZE = 1000


@dataclass
class Summary:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0


class MigrationRunner:
    def __init__(self, args):
        self.args = args
        self.summary = Summary()
        self.settings = SimpleNamespace(
            B2_APPLICATION_KEY_ID=os.environ.get("MM_B2_APPLICATION_KEY_ID"),
            B2_APPLICATION_KEY=os.environ.get("MM_B2_APPLICATION_KEY"),
        )
        self.api = MediaViewerClient(
            args.api_url,
            os.environ.get("MM_API_USERNAME"),
            os.environ.get("MM_API_PASSWORD"),
            os.environ.get("MM_API_TOKEN"),
        )
        self.run_id = None
        self.ownership_retained = False

    def run(self):
        if (not isinstance(self.args.batch_size, int) or isinstance(self.args.batch_size, bool)
                or not 1 <= self.args.batch_size <= MAX_BATCH_SIZE):
            raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
        if self.args.apply:
            if not getattr(self.args, "writers_paused", False):
                raise RuntimeError("apply requires writers-paused acknowledgement")
            if not (self.settings.B2_APPLICATION_KEY_ID and self.settings.B2_APPLICATION_KEY):
                raise RuntimeError("B2 credentials are required for apply")
            self.run_id = str(uuid.uuid4())
            try:
                self.api.acquire_run(self.run_id)
            except Exception:
                print(f"MediaViewer run ownership may have been acquired; run_id={self.run_id}. Inspect active ownership before recovery.", file=sys.stderr)
                raise
            self.ownership_retained = True
        try:
            records = self.api.candidates(self.args.batch_size, self.args.bucket, self.args.prefix)
            self._run_candidates(records)
        except Exception:
            if self.args.apply:
                print(f"MediaViewer run ownership retained; run_id={self.run_id}. Confirm the old process and uploads stopped before manual recovery.", file=sys.stderr)
            raise
        if self.args.apply:
            if self.summary.failed:
                print(f"MediaViewer run ownership retained; run_id={self.run_id}. Confirm the old process and uploads stopped before manual recovery.", file=sys.stderr)
            else:
                try:
                    self.api.release_run(self.run_id)
                    self.ownership_retained = False
                except Exception:
                    print(f"MediaViewer run ownership retained; run_id={self.run_id}. Confirm the old process and uploads stopped before manual recovery.", file=sys.stderr)
                    raise
        return self.summary

    def _run_candidates(self, records):
        for raw in records:
            pk = raw.get("pk", "unknown") if isinstance(raw, dict) else "unknown"
            self.summary.attempted += 1
            try:
                if not isinstance(raw, dict):
                    raise TypeError("candidate must be an object")
                if "error" in raw:
                    raise ValueError(str(raw["error"]))
                parent = self._record(raw)
                plan = self._plan(parent)
                if self.args.apply:
                    self._execute(plan)
                else:
                    print(f"would migrate MediaPath {parent.pk}")
                self.summary.succeeded += 1
            except Exception as exc:  # noqa: BLE001 - record failures must not stop the bounded batch
                self.summary.failed += 1
                print(f"MediaPath {pk}: {exc.__class__.__name__}: {safe_message(exc, self.settings)}", file=sys.stderr)
        return self.summary

    @staticmethod
    def _record(raw):
        required = ("pk", "path", "tv", "movie", "filename", "subtitle_files", "children", "snapshot", "target")
        if any(key not in raw for key in required):
            raise ValueError("candidate is missing required fields")
        if (not isinstance(raw["pk"], int) or isinstance(raw["pk"], bool) or raw["pk"] < 1
                or not isinstance(raw["path"], str) or not raw["path"]):
            raise ValueError("candidate identity or path is malformed")
        if (not isinstance(raw["snapshot"], str) or not raw["snapshot"]
                or (raw["filename"] is not None and not isinstance(raw["filename"], str))):
            raise ValueError("candidate snapshot or filename is malformed")
        for key in ("tv", "movie"):
            value = raw[key]
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise ValueError("candidate TV/movie identity is malformed")
        if (raw["tv"] is None) == (raw["movie"] is None):
            raise ValueError("candidate must reference exactly one TV or movie")
        if not isinstance(raw["children"], list) or not isinstance(raw["target"], dict):
            raise TypeError("candidate children or target is malformed")
        target = raw["target"]
        if any(not isinstance(target.get(k), str) or not target[k] for k in ("bucket", "path", "b2_upload_prefix")):
            raise ValueError("candidate B2 target is malformed")
        children = []
        child_ids = set()
        for child in raw["children"]:
            if not isinstance(child, dict) or not all(k in child for k in ("pk", "filename", "b2_uuid", "size", "subtitle_files")):
                raise ValueError("candidate child is malformed")
            child_pk = child["pk"]
            if not isinstance(child_pk, int) or isinstance(child_pk, bool) or child_pk < 1 or child_pk in child_ids:
                raise ValueError("candidate child identity is malformed or duplicated")
            if not isinstance(child["filename"], str) or not child["filename"]:
                raise ValueError("candidate child filename is malformed")
            if child["b2_uuid"] is not None and not isinstance(child["b2_uuid"], str):
                raise ValueError("candidate child B2 identity is malformed")
            if child["size"] is not None and (not isinstance(child["size"], int)
                                               or isinstance(child["size"], bool) or child["size"] < 0):
                raise ValueError("candidate child size is malformed")
            if child["subtitle_files"] is not None and not isinstance(child["subtitle_files"], list):
                raise ValueError("candidate child subtitles are malformed")
            child_ids.add(child_pk)
            children.append(SimpleNamespace(**child))
        return SimpleNamespace(
            pk=raw["pk"], _path=raw["path"], tv_id=raw.get("tv"), movie_id=raw.get("movie"),
            filename=raw["filename"], subtitle_files=raw["subtitle_files"], children=children,
            snapshot=raw["snapshot"], target=target,
        )

    def _plan(self, parent):
        if bool(parent.tv_id) == bool(parent.movie_id):
            raise ValueError("path must reference exactly one TV or movie record")
        if parent.filename and parent.tv_id:
            raise ValueError("TV path cannot have a movie filename")
        root = Path(parent._path)
        if not root.exists() or not root.is_dir():
            raise ValueError("media directory is missing")
        root = root.resolve(strict=True)
        children = parent.children
        if any(getattr(child, "b2_uuid", None) for child in children):
            raise ValueError("legacy path has partial B2 metadata")
        if parent.tv_id and not children:
            raise ValueError("TV path has no episodes")
        selected = []
        if parent.tv_id:
            for child in children:
                selected.append((child, self._contained(root, child.filename)))
        else:
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
            subtitles = self._subtitles(root, video, [*(parent_subtitles or []), *(child_subtitles or [])])
            from mediamigrate.media import inspect_video, source_fingerprint
            inspect_video(video, subtitles)
            fingerprint = source_fingerprint(video, subtitles)
            plans.append({"child": child, "video": video, "subtitles": subtitles, "fingerprint": fingerprint})
        return {"parent": parent, "children": children, "plans": plans, "root": root}

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
            if not any(base == stem or base.startswith((stem + ".", stem + "-")) for stem in stems):
                continue
            if candidate.suffix.lower() != ".vtt":
                if candidate.suffix.lower() in {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"}:
                    raise ValueError("matched subtitle sidecar must be WebVTT")
                continue
            names.append(candidate.name)
        return [self._contained(root, name) for name in sorted(set(names))]

    def _execute(self, plan):
        from mediamigrate.media import prepare_video
        parent = plan["parent"]
        prepared = []
        for item in plan["plans"]:
            result = prepare_video(item["video"], item["subtitles"])
            if (Path(result.video).resolve() != item["video"]
                    or tuple(Path(p).resolve() for p in result.subtitles) != tuple(item["subtitles"])
                    or result.fingerprint != item["fingerprint"]):
                raise RuntimeError("preparation must preserve original media files")
            prepared.append((item, result))
        verify_sources(plan)
        from .b2 import B2Client
        client = B2Client(self.settings.B2_APPLICATION_KEY_ID, self.settings.B2_APPLICATION_KEY,
                          os.environ.get("MM_B2_API_URL"))
        target = parent.target
        bucket, prefix = target["bucket"], target["b2_upload_prefix"]
        files = []
        for item, output in prepared:
            child = item["child"]
            identity = f"{parent.pk}:mediafile:{child.pk}" if child else f"{parent.pk}:movie:{parent.movie_id}"
            uid = str(object_uuid(identity, item["fingerprint"], output.video))
            upload_verified(client, output.video, bucket, f"{prefix}{uid}{item['video'].suffix}")
            subtitle_ids = []
            for subtitle in output.subtitles:
                sid = str(object_uuid(identity, item["fingerprint"], subtitle))
                upload_verified(client, subtitle, bucket, f"{prefix}{sid}.vtt")
                subtitle_ids.append(f"{sid}.vtt")
            files.append({"pk": child.pk if child else None, "filename": item["video"].name,
                          "b2_uuid": uid, "size": Path(output.video).stat().st_size,
                          "subtitle_files": subtitle_ids})
        manifest = {"run_id": self.run_id, "snapshot": parent.snapshot, "files": files}
        verify_sources(plan)
        self.api.publish(parent.pk, manifest)


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
    for name in ("MM_API_PASSWORD", "MM_API_TOKEN", "MM_B2_APPLICATION_KEY_ID", "MM_B2_APPLICATION_KEY"):
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:240] or error.__class__.__name__
