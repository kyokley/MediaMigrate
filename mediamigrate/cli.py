import argparse
import re
import sys
from pathlib import Path

from .runner import MigrationRunner


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("batch size must be a positive integer")
    return number


def parser():
    result = argparse.ArgumentParser(prog="mediamigrate")
    result.add_argument("batch_size", type=positive_integer)
    result.add_argument("--mediaviewer", type=Path)
    result.add_argument("--settings", default="mysite.settings")
    result.add_argument("--bucket")
    result.add_argument("--prefix")
    result.add_argument("--apply", action="store_true", help="upload and publish; default is dry-run")
    result.add_argument(
        "--writers-paused",
        action="store_true",
        help="acknowledge filesystem and database writers stay paused through upload and publication",
    )
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.apply and not args.writers_paused:
        print("apply requires --writers-paused acknowledgement", file=sys.stderr)
        return 2
    if args.mediaviewer is None:
        args.mediaviewer = Path(__file__).resolve().parents[2] / "MediaViewer"
    runner = None
    try:
        runner = MigrationRunner(args)
        summary = runner.run()
    except Exception as exc:  # noqa: BLE001 - render sanitized fatal diagnostics
        print(f"mediamigrate: {safe_error(exc, getattr(runner, 'settings', None))}", file=sys.stderr)
        return 1
    print(f"attempted={summary.attempted} succeeded={summary.succeeded} failed={summary.failed}")
    return 1 if summary.failed else 0


def safe_error(error, settings=None):
    # Avoid exposing credentials, signed URLs, or exception data from HTTP clients.
    message = str(error).split("?", 1)[0]
    message = re.sub(r"https?://\S+", "[URL]", message)
    for name in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY"):
        secret = getattr(settings, name, None) if settings is not None else None
        if secret:
            message = message.replace(str(secret), "[REDACTED]")
    return message[:300] if settings is not None else error.__class__.__name__
