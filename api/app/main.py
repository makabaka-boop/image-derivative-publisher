"""FastAPI service: uploads, crop-job management and published-version reads.

Concurrency invariants enforced here and in the worker:

* A job has exactly one terminal state.  Every transition (claim, succeed,
  fail, cancel) is a conditional ``UPDATE ... WHERE status IN (...)`` so a
  cancel/complete race is decided by whichever statement commits first.
* ``images.published_*`` only ever moves forward to a strictly greater
  version, inside the same transaction that marks the job succeeded, so a
  late-finishing stale job can never overwrite a newer published selection.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .db import (
    ACTIVE_STATUSES,
    ALLOWED_CONTENT_TYPES,
    DERIVATIVE_SIZES,
    MAX_UPLOAD_BYTES,
    MEDIA_DIR,
    ORIGINALS_DIR,
    connect,
    init_db,
    tx,
    utcnow,
)
from .imaging import BadImageError, decode_image, validate_crop

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="derivative-service", lifespan=lifespan)


# --------------------------------------------------------------------------
# serialisation helpers
# --------------------------------------------------------------------------

def job_dict(row, published_job_id: int | None = None) -> dict:
    job = {
        "id": row["id"],
        "image_id": row["image_id"],
        "crop": {"x": row["crop_x"], "y": row["crop_y"], "w": row["crop_w"], "h": row["crop_h"]},
        "version": row["version"],
        "status": row["status"],
        "attempts": row["attempts"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
        "published": published_job_id == row["id"],
        "derivative_urls": None,
    }
    if published_job_id == row["id"] and row["out_256"] and row["out_512"]:
        job["derivative_urls"] = {
            str(size): f"/media/derivatives/{os.path.basename(row[f'out_{size}'])}"
            for size in DERIVATIVE_SIZES
        }
    return job


def image_dict(row, conn) -> dict:
    published = None
    if row["published_job_id"] is not None:
        job_row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (row["published_job_id"],)
        ).fetchone()
        if job_row is not None:
            published = job_dict(job_row, published_job_id=row["published_job_id"])
    return {
        "id": row["id"],
        "sha256": row["sha256"],
        "width": row["width"],
        "height": row["height"],
        "original_url": f"/media/originals/{os.path.basename(row['path'])}",
        "version_counter": row["version_counter"],
        "published_version": row["published_version"],
        "published": published,
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------

@app.post("/api/images", status_code=201)
async def upload_image(file: UploadFile = File(...)):
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content type {file.content_type!r}; use PNG or JPEG",
        )

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1 << 16):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="file exceeds 4 MiB limit")
        hasher.update(chunk)
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="empty file")
    data = b"".join(chunks)

    try:
        img = decode_image(data)
    except BadImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    digest = hasher.hexdigest()
    ext = ".png" if img.format == "PNG" else ".jpg"
    rel_path = os.path.join(ORIGINALS_DIR, f"{digest}{ext}")

    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO images (sha256, width, height, path, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(sha256) DO NOTHING""",
            (digest, img.width, img.height, rel_path, utcnow()),
        )
        created = cur.rowcount > 0
        row = conn.execute("SELECT * FROM images WHERE sha256 = ?", (digest,)).fetchone()
        if not os.path.exists(rel_path):
            # Unique tmp name: concurrent uploads of the same digest can never
            # rename each other's half-written file.
            tmp_path = f"{rel_path}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            os.replace(tmp_path, rel_path)
        return image_dict(row, conn) | {"created": created}


# --------------------------------------------------------------------------
# images / jobs
# --------------------------------------------------------------------------

class CropRequest(BaseModel):
    x: int = Field(..., ge=0)
    y: int = Field(..., ge=0)
    w: int = Field(..., ge=0)
    h: int = Field(..., ge=0)


@app.get("/api/images/{image_id}")
def get_image(image_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="image not found")
        return image_dict(row, conn)


@app.get("/api/images/{image_id}/jobs")
def list_jobs(image_id: int):
    with connect() as conn:
        image = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if image is None:
            raise HTTPException(status_code=404, detail="image not found")
        rows = conn.execute(
            "SELECT * FROM jobs WHERE image_id = ? ORDER BY id", (image_id,)
        ).fetchall()
        return {"jobs": [job_dict(r, published_job_id=image["published_job_id"]) for r in rows]}


@app.post("/api/images/{image_id}/jobs", status_code=201)
def create_job(image_id: int, req: CropRequest):
    with tx() as conn:
        image = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if image is None:
            raise HTTPException(status_code=404, detail="image not found")

        problem = validate_crop(req.x, req.y, req.w, req.h, image["width"], image["height"])
        if problem:
            raise HTTPException(status_code=422, detail=problem)

        recipe = (image_id, req.x, req.y, req.w, req.h)
        existing = conn.execute(
            """SELECT * FROM jobs
               WHERE image_id = ? AND crop_x = ? AND crop_y = ? AND crop_w = ? AND crop_h = ?""",
            recipe,
        ).fetchone()

        now = utcnow()
        if existing is not None and existing["status"] in ACTIVE_STATUSES + ("succeeded",):
            # Same digest + same crop: reuse the in-flight or completed job.
            return {
                "job": job_dict(existing, published_job_id=image["published_job_id"]),
                "reused": True,
            }

        # New selection (or re-attempt of a failed/canceled recipe): it becomes
        # the latest version so that "last request wins" holds on publish.
        version = image["version_counter"] + 1
        conn.execute(
            "UPDATE images SET version_counter = ? WHERE id = ?", (version, image_id)
        )
        if existing is not None:
            conn.execute(
                """UPDATE jobs
                   SET status = 'queued', version = ?, attempts = 0, error = NULL,
                       out_256 = NULL, out_512 = NULL, finished_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (version, now, existing["id"]),
            )
            job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (existing["id"],)).fetchone()
        else:
            cur = conn.execute(
                """INSERT INTO jobs (image_id, crop_x, crop_y, crop_w, crop_h, version,
                                     status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (image_id, req.x, req.y, req.w, req.h, version, now, now),
            )
            job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"job": job_dict(job_row, published_job_id=image["published_job_id"]), "reused": False}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        image = conn.execute("SELECT * FROM images WHERE id = ?", (row["image_id"],)).fetchone()
        return {"job": job_dict(row, published_job_id=image["published_job_id"])}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    with tx() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        # Conditional transition: exactly one of cancel / complete can win.
        now = utcnow()
        cur = conn.execute(
            """UPDATE jobs SET status = 'canceled', finished_at = ?, updated_at = ?
               WHERE id = ? AND status IN ('queued', 'processing')""",
            (now, now, job_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail=f"job already terminal ({row['status']})",
            )
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        image = conn.execute("SELECT * FROM images WHERE id = ?", (row["image_id"],)).fetchone()
        return {"job": job_dict(row, published_job_id=image["published_job_id"])}


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


# --------------------------------------------------------------------------
# media files (originals + published derivatives)
# --------------------------------------------------------------------------

@app.get("/media/{kind}/{filename}")
def media(kind: str, filename: str):
    if kind not in ("originals", "derivatives"):
        raise HTTPException(status_code=404, detail="not found")
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="bad filename")
    path = os.path.join(MEDIA_DIR, kind, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)
