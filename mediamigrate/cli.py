import argparse
import os
import re
import sys

from .runner import MigrationRunner

MAX_BATCH_SIZE = 1000


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be a positive integer") from exc
    if not 1 <= number <= MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    return number


def parser():
    result = argparse.ArgumentParser(prog="mediamigrate")
    result.add_argument("batch_size", type=positive_integer)
    result.add_argument("--api-url", default=os.environ.get("MM_API_URL"))
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
    secrets = [os.environ.get(name) for name in (
        "MM_API_PASSWORD", "MM_API_TOKEN", "MM_B2_APPLICATION_KEY_ID", "MM_B2_APPLICATION_KEY"
    )]
    for name in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY"):
        secret = getattr(settings, name, None) if settings is not None else None
        if secret:
            secrets.append(str(secret))
    for secret in secrets:
        if secret:
            message = message.replace(str(secret), "[REDACTED]")
    return message[:300] or error.__class__.__name__
