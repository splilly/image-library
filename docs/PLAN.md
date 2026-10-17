# Plan

## Goals

A standalone, portable (Windows / macOS / Linux) application to catalog many TB of
photos and videos spread over drives that are connected only some of the time, some
of them only readable from one OS. It must:

* track progress accurately and survive crashes, with long-running work resumable;
* work with only some drives connected, and say which drive holds an image (with preview);
* find corrupt images and let the user look at them before discarding;
* find exact duplicates and thumbnails generated from originals by old software,
  distinguishing thumbnails whose original still exists from orphans that are now
  the only surviving copy;
* never require installing a runtime or other software first.

## Stack

| Layer | Choice |
|---|---|
| Engine | Python 3.12, packaged with PyInstaller per OS (no runtime install) |
| Decoding | Pillow, pillow-heif, rawpy (LibRaw), psd-tools, PyAV (bundled ffmpeg) |
| Metadata | exiv2 (bundled C++ library; no Perl/exiftool dependency) |
| Hashing | BLAKE3 for identity; 64-bit pHash + dHash (numpy) for similarity |
| Catalog | SQLite (WAL): `catalog.db` + `previews.db` |
| API / GUI | FastAPI + WebSocket progress; React + TypeScript (Vite), virtualized grids |
| Desktop shell | pywebview window, or the default browser |
| Builds | GitHub Actions matrix: Windows x64, macOS arm64 + x64, Linux x64 |

## Key decisions

* **Volume identity** — a `.imagelib-volume.json` marker (UUID + label) at the drive
  root. Paths stored relative to it with `/` separators; NFC + casefold `path_key`
  for cross-OS matching; raw bytes kept for non-UTF-8 names.
* **Content-addressed analysis** — one `contents` row per unique BLAKE3 hash, so
  duplicates are decoded once and previews are shared.
* **Read once** — each file is read sequentially by its drive's reader and the same
  bytes are hashed, decoded (integrity check), measured, perceptually hashed and
  previewed.
* **Resumability** — per-file state in SQLite; results committed every second; a
  scan record per volume/sub-folder resumes where it stopped. Unplugging a drive
  pauses only that drive; files are never marked missing unless a walk completed
  with the marker still present.
* **Robust decoding** — a decoder crash brings down the pool; in-flight files are
  re-run one at a time to find the culprit. Hangs are killed after a timeout. Both
  are recorded as corrupt instead of stopping the scan.
* **Safety** — no automatic deletions. Decisions become a reviewable action plan,
  executed by moving files into `_imagelib_quarantine/` on the same drive (instant,
  reversible). The last verified good copy of any content can never be discarded.
* **Multi-machine: hub + agents** — one hub holds the main catalog and GUI. Agents
  scan their locally attached drives and sync results and previews to the hub over
  the LAN, or via an exported bundle file when offline. Quarantine actions for
  agent-only drives are relayed to that agent.

## Thumbnail classification (milestone 4)

Signals: small dimensions; cache-style paths (`.thumbnails`, `@eaDir`, `tn_`, `_thumb`,
Picasa/iPhoto/ACDSee/Lightroom preview folders); missing EXIF; Software tag; a pHash
match to a larger image with the same aspect ratio. Outcome per thumbnail:

* original found and verified OK (on which drives) → safe to discard;
* original found but corrupt, or only on an offline drive → user decides;
* no original found → orphan; kept by default and marked "only surviving copy".

## Milestones

1. **Catalog + scanning engine (done)** — schema, drive registry, discovery,
   read-once analysis, resume, offline handling, crash isolation, CLI, sample library, tests.
2. **API + GUI (done)** — dashboard with live per-drive throughput/ETA, drives page, browse
   and search with previews, drag-an-image-in "where is this?".
3. **Corrupt review + exact duplicates review (done)** — keyboard-driven review screens,
   duplicate-folder pairs, decisions with last-copy protection.
4. **Near-duplicates + thumbnail detection (done)** — pixel hashes, relate job,
   thumbnail classification (incl. "not certain"), similar-image review.
5. **Action plan (done)** — quarantine with per-file re-checks at execution time,
   journaled moves, restore, typed-confirmation purge, lists for read-only drives.
6. **Hub/agent sync + remote access (done)** — remote access to the GUI; agents sync
   incrementally over Tailscale/Cloudflare with per-agent tokens, or via bundle files;
   plan actions (move/restore/purge) relayed to the agent that holds the drive.
7. **Packaging (done)** — PyInstaller one-folder builds for all OSes via GitHub Actions
   with a smoke test of each packaged app; Organize copies the best unique originals
   into a date-sorted library.

## Remote access (done, ahead of the rest of milestone 6)

Drive the GUI from another device (phone, laptop elsewhere). The API can browse
folders, stream photos and move/delete files, so it must never be exposed without
authentication. Two layers:

* **Transport**, one of:
  * *Tailscale* (recommended for personal use): devices join a private network;
    nothing is exposed to the internet; HTTPS via `tailscale serve`.
  * *Cloudflare Tunnel + Cloudflare Access*: a public HTTPS hostname, but Cloudflare
    requires the owner's login (Google / one-time email code) before any request
    reaches the app. The app verifies the `Cf-Access-Jwt-Assertion` header.
  * Never a bare quick tunnel (`trycloudflare.com`): the URL alone would be enough.
* **App login** for every request not coming from the local machine (passkey or
  password, long-lived session cookie, strict SameSite), so a misconfigured tunnel is
  not the only line of defence. Agents use per-device tokens issued by the hub.
* Off by default; enabled in settings, which also shows which devices are signed in.
