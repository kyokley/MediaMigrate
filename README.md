# MediaMigrate

Migrate legacy MediaViewer movie and TV storage to Backblaze B2 without
replacing the original files or recreating library records.

## Safety and scope

- A batch counts **MediaPath records**, not individual movies, episodes, or
  upload requests. One TV path can contain many episodes.
- All episodes on a TV path, including hidden episodes, must migrate before
  its shared path changes to B2. Hidden flags and record IDs remain unchanged.
- Existing MediaViewer-encoded video and WebVTT subtitles upload byte for
  byte. This tool does not encode, probe codecs, or require FFmpeg. Source
  compatibility is assumed; source files remain untouched.
- MediaViewer must be available locally with its Django dependencies,
  configured database, and access to the original media paths. No new public
  migration API is needed.
- Migration uses the existing MediaViewer B2 settings and its object-key
  layout. Do not point development verification at a production bucket.
- Database backups and independent playback checks remain the operator's
  responsibility. Do not delete original files merely because migration
  reports success.

## Requirements

Use Python 3.11 or newer in the environment containing MediaViewer and its dependencies, with
access to its Django settings and PostgreSQL database. Use the accompanying
MediaViewer B2 client updates. No new database migration is required.

Configure MediaViewer's existing `B2_APPLICATION_KEY_ID`,
`B2_APPLICATION_KEY`, `B2_BUCKET_NAME`, `B2_NAME_PREFIX`, and optionally
`B2_API_URL`. The application key needs `writeFiles`, `listFiles`, and
`shareFiles`. Use a private bucket with the same playback/CORS configuration
as existing MediaViewer B2 media. Never put keys in command-line arguments.

Source files must already be encoded for MediaViewer. Subtitles must already
be WebVTT; unsupported subtitle formats fail rather than being renamed or
silently discarded. Keep source files unchanged throughout migration.
TV parent-level subtitle metadata is rejected because it cannot be safely
assigned to episodes; store explicit TV caption references on each MediaFile.

## Run a batch

Run from this repository using MediaViewer's Python environment:

```sh
python -m mediamigrate 10 \
  --mediaviewer ../MediaViewer \
  --settings mysite.settings
```

This is a **dry run**: inspect candidates and validate file references without
uploading or changing records. The integer is a maximum number of records
attempted, not a target number of successful migrations. Failed records count
toward that limit. Records are considered in primary-key order; already-B2
and skipped paths are excluded.

Before applying, back up the database and pause MediaConverter, library
scanners, the existing `upload_to_b2` command, and any other process that
changes the selected media files or records. Source files must remain
immutable during hashing, upload, verification, and publication. Then
explicitly acknowledge this:

```sh
python -m mediamigrate 10 \
  --mediaviewer ../MediaViewer \
  --settings mysite.settings \
  --apply --writers-paused
```

One migrator can apply at a time per PostgreSQL database. The advisory lock
does not stop other tools from writing: the pause is a real operating
requirement, not an automatic maintenance mode. The final database transaction
rejects changes to the inspected record inventory or source files.
Use a stable PostgreSQL session, not a transaction-pooling connection.
Integrity checks reread complete source files, including a final check while
database rows are locked; allow for this when planning the maintenance window.

## Failure and recovery

Rerun with the same settings and unchanged originals. Completed remote
objects are reused only when the exact key, size, and checksum match.
Malformed listing responses, conflicting versions, hide markers, or
unfinished multipart uploads fail closed rather than overwrite media or
start another billable upload blindly.

An upload and a database commit cannot form one distributed transaction.
Completed orphan objects and unfinished multipart parts may remain billable
after interruption. Inspect them in Backblaze's console before any cleanup;
never delete an object referenced by published records. Resolve unfinished
uploads deliberately before retrying. This tool does not delete remote
objects or source files.

Unsupported subtitles, missing files, ambiguous movie selection, and
unsafe paths block that record. Fix the source inventory before retrying;
do not bypass failed checks merely to move the batch forward.
Symbolic-link media files are rejected, including links within the same directory.

Exit status is `0` for a successful batch (including an empty batch), `1` for
migration failures, and `2` for invalid CLI arguments or a missing apply
acknowledgement. The summary reports attempted, succeeded, and failed counts;
in dry-run mode, succeeded means validation passed, not that data was moved.

After success, open a migrated movie and TV episode in MediaViewer's B2
player. Check seeking, sound, captions, resume progress, and hidden-episode
metadata before resuming writers. Cast/device compatibility and production
B2 permissions require separate verification in the target environment.

## Tests

Run `python -m pytest -q` in the MediaViewer Python environment with pytest
and pytest-django installed. Pure validation and storage tests need no media
tools. Database integration tests require explicitly configured isolated
PostgreSQL settings named `mv_test_settings`, `DJANGO_SETTINGS_MODULE` set to
that module, and `MV_ROOT` pointing at the MediaViewer checkout. Put both the
isolated settings module and MediaViewer on `PYTHONPATH`. These tests create
and destroy a test database; never configure them against production.

The HTTP integration fixture uses an ephemeral loopback server and temporary
media files. It does not access a real B2 account or verify browser decoding.
