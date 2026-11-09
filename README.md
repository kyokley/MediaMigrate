# MediaMigrate

Migrate legacy MediaViewer movie and TV storage to Backblaze B2 without
replacing original files or recreating library records. MediaMigrate accesses
MediaViewer only through its REST API; it does not require a MediaViewer
checkout, Django, or a local database connection.

## Safety and prerequisites

- A batch counts candidate `MediaPath` records, not individual episodes or
  upload requests. Failed candidates count toward the batch limit.
- The API supplies a bounded, primary-key-ordered inventory. TV children,
  including hidden episodes, are included and published together.
- Existing encoded video and WebVTT subtitles upload byte for byte. Sources
  remain unchanged; FFmpeg is not required. Source paths returned by the API
  must be reachable at the same paths on the migration host.
- The deployment must provide the `migrations/run/`,
  `migrations/candidates/`, and `migrations/{pk}/publish/` endpoints, including
  durable single-run ownership and atomic snapshot-CAS publication. Deploy the
   migration API and schema update in MediaViewer,
  then run `python manage.py migrate` in the MediaViewer server deployment
  before using this client. These backend changes are not part of this client
  package.
- Authenticate with staff Basic Auth or an `Authorization: Token ...` token.
  Token authentication works only when the server has token authentication and
   its token model configured. Basic Auth works with the default server setup. TLS
  verification is enabled. HTTP is accepted only for loopback development
  endpoints. Never put credentials in command-line arguments.
- Keep filesystem/database writers paused throughout apply. The ownership
  record coordinates migrators; it does not pause writers itself.
- Do not delete source files after success. Independently verify playback and
  backups before resuming production writers.

Install standalone, with pytest for development:

```sh
python -m pip install -e '.[dev]'
```

Configure credentials through environment variables:

```sh
export MM_API_URL='https://viewer/mediaviewer/api/'
export MM_API_USERNAME='staff-user'
export MM_API_PASSWORD='secret'
# Alternatively, use a token instead of Basic Auth:
# export MM_API_TOKEN='configured-server-token'
export MM_B2_APPLICATION_KEY_ID='key-id'
export MM_B2_APPLICATION_KEY='key-secret'
export MM_B2_API_URL='https://api.backblazeb2.com' # optional
```

The B2 application key needs upload and file-list permissions for the selected
bucket. Bucket and object prefix normally come from each API candidate's
server-defined target. Optional `--bucket` and `--prefix` override those API
target choices. Sharding is always determined by the server.

## Dry run and apply

```sh
python -m mediamigrate 10
```

Dry run validates local source files and candidate metadata. It does not acquire
run ownership, access B2, or publish changes. The number is the maximum number
of API candidates processed, including failures.

Pause all media and database writers, then explicitly acknowledge the pause:

```sh
python -m mediamigrate 10 --apply --writers-paused
```

Apply creates a run UUID and acquires durable singleton ownership before
fetching candidates. The client verifies source fingerprints immediately
before publication. The server must validate run ownership and the opaque
snapshot token, then atomically publish the complete manifest. No migration
record is changed through separate CRUD requests. B2 uploads are direct from
the client and verified by exact key, size, and checksum before publication.

## Failure and recovery

Ownership is released only when every candidate in the acquired batch completes
successfully. Any candidate failure or uncertain request outcome retains the
ownership record for manual recovery. The diagnostic prints the run UUID. Do
not clear ownership until confirming the old process and its uploads have
stopped. Inspect active ownership, then release the matching run only after
reconciling published records and remote objects:

```sh
curl --fail --user "$MM_API_USERNAME:$MM_API_PASSWORD" "${MM_API_URL%/}/migrations/run/"
curl --fail --user "$MM_API_USERNAME:$MM_API_PASSWORD" \
  -X DELETE -H 'Content-Type: application/json' \
  --data '{"run_id":"UUID-FROM-INSPECTION"}' "${MM_API_URL%/}/migrations/run/"
```

For token auth, replace `--user` with
`-H "Authorization: Token $MM_API_TOKEN"`. API redirects are refused to
prevent credential forwarding. Requests have bounded timeouts and are never
automatically retried. An ambiguous publication failure may have committed
server-side; reconcile records before any manual release or rerun.

Completed orphan B2 objects or unfinished multipart parts may remain after an
interruption. Inspect them before cleanup; never delete objects referenced by
published records. Unsafe paths, missing/empty files, ambiguous movie
selection, unsupported subtitles, and changed source bytes fail closed.

Exit status is `0` for a successful batch, `1` for migration/API failures, and
`2` when apply lacks `--writers-paused` or arguments are invalid. In dry-run
mode, succeeded means validation passed, not that data moved.

## Tests

Run the standalone test suite without MediaViewer, Django, a database, or live
services:

```sh
python -m pytest -q
```
