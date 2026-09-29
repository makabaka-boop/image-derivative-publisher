"""Derivative worker process.

Loop: claim the oldest queued job -> render 256px and 512px WebP files into
the tmp directory -> atomically rename both into the derivatives directory ->
commit a single DB transaction that (a) flips the job to ``succeeded`` only
if it is still active (losing the race against a cancel) and (b) advances
the image's published pointer only if this job's version is newer.

Crash safety: on startup the worker deletes every leftover tmp file and
re-queues jobs that were stuck in ``processing`` so unfinished work retries.

Test hooks (documented in README): while ``$DATA_DIR/flags/skip_all`` exists
no job is claimed; ``$DATA_DIR/flags/skip_job_<id>`` excludes one job from
claiming; while ``$DATA_DIR/flags/pause_mid_all`` exists the worker pauses
after writing the first tmp derivative.  The verify suite uses these to
build deterministic races.
"""

from __future__ import annotations

import os
import sys
import time
import traceback

from .db import (
    DATA_DIR,
    DERIVATIVE_SIZES,
    DERIVATIVES_DIR,
    FLAGS_DIR,
    TMP_DIR,
    connect,
    init_db,
    tx,
    utcnow,
)
from .imaging import decode_image, render_derivative, save_webp

POLL_INTERVAL = float(os.environ.get("WORKER_POLL_INTERVAL", "0.5"))
HEARTBEAT_PATH = os.path.join(DATA_DIR, "worker.heartbeat")

SKIP_ALL_FLAG = os.path.join(FLAGS_DIR, "skip_all")
PAUSE_MID_FLAG = os.path.join(FLAGS_DIR, "pause_mid_all")


def flag_exists(path: str) -> bool:
    return os.path.exists(path)


def recover() -> None:
    """Crash recovery: clean tmp files, re-queue interrupted jobs."""
    removed = 0
    for name in os.listdir(TMP_DIR):
        try:
            os.remove(os.path.join(TMP_DIR, name))
            removed += 1
        except OSError:
            pass
    with tx() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status = 'processing'",
            (utcnow(),),
        )
        requeued = cur.rowcount
    print(f"[worker] recovery: removed {removed} tmp file(s), re-queued {requeued} job(s)",
          flush=True)


def skipped_job_ids() -> set[int]:
    ids: set[int] = set()
    try:
        for name in os.listdir(FLAGS_DIR):
            if name.startswith("skip_job_"):
                try:
                    ids.add(int(name[len("skip_job_"):]))
                except ValueError:
                    pass
    except OSError:
        pass
    return ids


def claim_next():
    """Atomically claim the oldest queued job (None if nothing to do)."""
    if flag_exists(SKIP_ALL_FLAG):
        return None
    skipped = skipped_job_ids()
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 8"
        ).fetchall()
        row = next((r for r in rows if r["id"] not in skipped), None)
        if row is None:
            return None
        cur = conn.execute(
            """UPDATE jobs SET status = 'processing', attempts = attempts + 1,
                              updated_at = ?
               WHERE id = ? AND status = 'queued'""",
            (utcnow(), row["id"]),
        )
        if cur.rowcount == 0:
            return None  # lost a race (e.g. concurrent cancel)
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()


def tmp_path(job_id: int, size: int) -> str:
    return os.path.join(TMP_DIR, f"job-{job_id}-{size}.webp.tmp")


def final_path(job_id: int, size: int) -> str:
    return os.path.join(DERIVATIVES_DIR, f"job-{job_id}-{size}.webp")


def cleanup_tmp(job_id: int) -> None:
    for size in DERIVATIVE_SIZES:
        try:
            os.remove(tmp_path(job_id, size))
        except OSError:
            pass


def mark_failed(job_id: int, message: str) -> None:
    with tx() as conn:
        conn.execute(
            """UPDATE jobs SET status = 'failed', error = ?, finished_at = ?, updated_at = ?
               WHERE id = ? AND status IN ('queued', 'processing')""",
            (message[:500], utcnow(), utcnow(), job_id),
        )


def process(job) -> None:
    job_id = job["id"]
    print(f"[worker] processing job {job_id} (attempt {job['attempts']})", flush=True)
    try:
        with connect() as conn:
            image = conn.execute(
                "SELECT * FROM images WHERE id = ?", (job["image_id"],)
            ).fetchone()
        with open(image["path"], "rb") as fh:
            img = decode_image(fh.read())

        crop = (job["crop_x"], job["crop_y"], job["crop_w"], job["crop_h"])

        # 1. Render every size into the tmp directory first.
        for index, size in enumerate(DERIVATIVE_SIZES):
            derivative = render_derivative(img, crop, size)
            save_webp(derivative, tmp_path(job_id, size))
            if index == 0:
                while flag_exists(PAUSE_MID_FLAG):  # test hook: deterministic crash window
                    time.sleep(0.1)

        # 2. Only after both succeeded: move them into place atomically.
        finals = {}
        for size in DERIVATIVE_SIZES:
            os.replace(tmp_path(job_id, size), final_path(job_id, size))
            finals[size] = final_path(job_id, size)

        # 3. One transaction: terminal transition + publish-if-newer.
        with tx() as conn:
            now = utcnow()
            cur = conn.execute(
                """UPDATE jobs
                   SET status = 'succeeded', out_256 = ?, out_512 = ?,
                       error = NULL, finished_at = ?, updated_at = ?
                   WHERE id = ? AND status IN ('queued', 'processing')""",
                (finals[DERIVATIVE_SIZES[0]], finals[DERIVATIVE_SIZES[1]], now, now, job_id),
            )
            if cur.rowcount == 0:
                # A cancel (or failure) won the race; drop the files we just
                # moved so no orphan derivatives linger.
                won = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
                print(f"[worker] job {job_id} already terminal ({won['status']}); discarding output",
                      flush=True)
                for path in finals.values():
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                return
            conn.execute(
                """UPDATE images SET published_job_id = ?, published_version = ?
                   WHERE id = ? AND published_version < ?""",
                (job_id, job["version"], job["image_id"], job["version"]),
            )
        print(f"[worker] job {job_id} succeeded (version {job['version']})", flush=True)
    except Exception as exc:  # permanent failure for this attempt
        traceback.print_exc()
        cleanup_tmp(job_id)
        mark_failed(job_id, f"{type(exc).__name__}: {exc}")
        print(f"[worker] job {job_id} failed: {exc}", flush=True)


def main() -> int:
    init_db()
    recover()
    print(f"[worker] ready (poll={POLL_INTERVAL}s)", flush=True)
    while True:
        try:
            with open(HEARTBEAT_PATH, "w") as fh:
                fh.write(utcnow())
        except OSError:
            pass
        job = claim_next()
        if job is None:
            time.sleep(POLL_INTERVAL)
            continue
        process(job)


if __name__ == "__main__":
    sys.exit(main())
