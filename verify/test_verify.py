"""End-to-end verification for the derivative service.

Runs inside the ``verify`` compose service against the real API + worker.
Covers: duplicate requests, bad images, out-of-bounds crops, cancel races,
crash/restart recovery and stale-job write-back protection.
"""

from __future__ import annotations

import io
import os
import time

import httpx
import pytest
from PIL import Image

API_URL = os.environ.get("API_URL", "http://api:8000")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
FLAGS_DIR = os.path.join(DATA_DIR, "flags")
TMP_DIR = os.path.join(DATA_DIR, "media", "tmp")
DERIVATIVES_DIR = os.path.join(DATA_DIR, "media", "derivatives")

TERMINAL = {"succeeded", "failed", "canceled"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_image(fmt: str = "PNG", size: tuple[int, int] = (640, 480), color=(40, 120, 200)) -> bytes:
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((color[0] + x) % 256, (color[1] + y) % 256, (color[2] + x + y) % 256)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def upload(client: httpx.Client, data: bytes, content_type: str = "image/png",
           name: str = "sample.png") -> httpx.Response:
    return client.post("/api/images", files={"file": (name, data, content_type)})


def create_job(client: httpx.Client, image_id: int, crop: dict) -> dict:
    res = client.post(f"/api/images/{image_id}/jobs", json=crop)
    assert res.status_code == 201, res.text
    return res.json()


def get_job(client: httpx.Client, job_id: int) -> dict:
    res = client.get(f"/api/jobs/{job_id}")
    assert res.status_code == 200, res.text
    return res.json()["job"]


def get_image(client: httpx.Client, image_id: int) -> dict:
    res = client.get(f"/api/images/{image_id}")
    assert res.status_code == 200, res.text
    return res.json()


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.2, desc: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {desc}")


def wait_job_status(client: httpx.Client, job_id: int, statuses, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(client, job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached {statuses}; last={job['status']}")


def set_flag(name: str) -> str:
    path = os.path.join(FLAGS_DIR, name)
    os.makedirs(FLAGS_DIR, exist_ok=True)
    with open(path, "w") as fh:
        fh.write("1")
    return path


def clear_flag(name: str) -> None:
    try:
        os.remove(os.path.join(FLAGS_DIR, name))
    except FileNotFoundError:
        pass


def ls(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except FileNotFoundError:
        return []


@pytest.fixture(autouse=True)
def clean_flags():
    for name in ("skip_all", "pause_mid_all"):
        clear_flag(name)
    yield
    for name in ("skip_all", "pause_mid_all"):
        clear_flag(name)


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=API_URL, timeout=30.0) as c:
        wait_until(lambda: c.get("/api/health").status_code == 200,
                   timeout=60, desc="API health")
        yield c


def fresh_image(client: httpx.Client, fmt="PNG", size=(640, 480)) -> dict:
    # Unique pixel content per call -> unique digest -> fresh image row.
    seed = os.urandom(8)
    color = (seed[0], seed[1], seed[2])
    res = upload(client, make_image(fmt, size, color), f"image/{fmt.lower()}")
    assert res.status_code == 201, res.text
    return res.json()


def assert_derivative(client: httpx.Client, url: str, size: int):
    res = client.get(url)
    assert res.status_code == 200, f"{url}: {res.status_code}"
    img = Image.open(io.BytesIO(res.content))
    assert img.format == "WEBP"
    img.load()
    assert max(img.size) == size, f"expected max edge {size}, got {img.size}"


# ---------------------------------------------------------------------------
# 1. happy path: upload -> crop -> publish -> fetch derivatives
# ---------------------------------------------------------------------------

def test_publish_flow(client):
    image = fresh_image(client)
    assert image["width"] == 640 and image["height"] == 480
    assert image["published"] is None  # nothing published yet

    body = create_job(client, image["id"], {"x": 10, "y": 20, "w": 400, "h": 300})
    job = body["job"]
    assert body["reused"] is False
    assert job["status"] == "queued"
    assert job["version"] == 1

    wait_job_status(client, job["id"], {"succeeded"})

    wait_until(lambda: get_image(client, image["id"])["published"] is not None,
               desc="published version")
    image = get_image(client, image["id"])
    published = image["published"]
    assert published["id"] == job["id"]
    assert published["version"] == 1
    assert image["published_version"] == 1
    assert_derivative(client, published["derivative_urls"]["256"], 256)
    assert_derivative(client, published["derivative_urls"]["512"], 512)

    # JPEG originals work too.
    jpg = fresh_image(client, fmt="JPEG", size=(300, 200))
    body = create_job(client, jpg["id"], {"x": 0, "y": 0, "w": 300, "h": 200})
    wait_job_status(client, body["job"]["id"], {"succeeded"})


# ---------------------------------------------------------------------------
# 2. duplicate requests reuse completed derivatives; new params -> new version
# ---------------------------------------------------------------------------

def test_duplicate_request_reuses_results(client):
    image = fresh_image(client)
    crop = {"x": 0, "y": 0, "w": 320, "h": 240}

    first = create_job(client, image["id"], crop)["job"]
    wait_job_status(client, first["id"], {"succeeded"})

    # Identical digest + identical crop -> same job, no new version.
    again = create_job(client, image["id"], crop)
    assert again["reused"] is True
    assert again["job"]["id"] == first["id"]
    assert again["job"]["version"] == first["version"]
    assert again["job"]["status"] == "succeeded"
    assert get_image(client, image["id"])["version_counter"] == 1

    # Duplicate while a job is still queued/processing reuses it too.
    set_flag("skip_all")
    try:
        pending = create_job(client, image["id"], {"x": 1, "y": 1, "w": 100, "h": 100})["job"]
        dupe = create_job(client, image["id"], {"x": 1, "y": 1, "w": 100, "h": 100})
        assert dupe["reused"] is True and dupe["job"]["id"] == pending["id"]
    finally:
        clear_flag("skip_all")
    wait_job_status(client, pending["id"], {"succeeded"})

    # Changed crop parameters -> a new job with a bumped version.
    newer = create_job(client, image["id"], {"x": 5, "y": 5, "w": 200, "h": 200})["job"]
    assert newer["id"] != first["id"]
    assert newer["version"] > first["version"]
    wait_job_status(client, newer["id"], {"succeeded"})
    assert get_image(client, image["id"])["published_version"] == newer["version"]

    # Re-uploading the same bytes dedups the image itself.
    data = make_image("PNG", (640, 480), (123, 45, 67))
    r1 = upload(client, data)
    r2 = upload(client, data)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["created"] is False


# ---------------------------------------------------------------------------
# 3. bad images are rejected
# ---------------------------------------------------------------------------

def test_bad_image_rejected(client):
    # Garbage bytes with an image content type.
    res = upload(client, b"this is definitely not an image" * 10)
    assert res.status_code == 400, res.text

    # Truncated PNG header.
    res = upload(client, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert res.status_code == 400, res.text

    # Wrong content type is rejected before decoding.
    res = upload(client, make_image("PNG", (32, 32)), "image/gif", "x.gif")
    assert res.status_code == 415, res.text
    res = upload(client, b"hello", "text/plain", "x.txt")
    assert res.status_code == 415, res.text

    # Over the 4 MiB limit.
    res = upload(client, os.urandom(4 * 1024 * 1024 + 1))
    assert res.status_code == 413, res.text

    # A real but unsupported format (GIF bytes are decodable by Pillow).
    gif = make_image("GIF", (32, 32))
    res = upload(client, gif, "image/gif", "x.gif")
    assert res.status_code == 415, res.text


# ---------------------------------------------------------------------------
# 4. crop rectangle must stay inside the image
# ---------------------------------------------------------------------------

def test_crop_out_of_bounds(client):
    image = fresh_image(client)  # 640x480
    bad_crops = [
        {"x": -1, "y": 0, "w": 100, "h": 100},
        {"x": 0, "y": -5, "w": 100, "h": 100},
        {"x": 0, "y": 0, "w": 0, "h": 100},
        {"x": 0, "y": 0, "w": 100, "h": 0},
        {"x": 100, "y": 0, "w": 541, "h": 100},   # x + w > 640
        {"x": 0, "y": 100, "w": 100, "h": 381},   # y + h > 480
        {"x": 640, "y": 0, "w": 1, "h": 1},
        {"x": 0, "y": 480, "w": 1, "h": 1},
        {"x": 0, "y": 0, "w": 10_000, "h": 10_000},
    ]
    for crop in bad_crops:
        res = client.post(f"/api/images/{image['id']}/jobs", json=crop)
        assert res.status_code == 422, f"{crop}: {res.status_code} {res.text}"

    # The exact full frame is legal.
    res = client.post(f"/api/images/{image['id']}/jobs",
                      json={"x": 0, "y": 0, "w": 640, "h": 480})
    assert res.status_code == 201, res.text
    # Edge-hugging 1px crop is legal too.
    res = client.post(f"/api/images/{image['id']}/jobs",
                      json={"x": 639, "y": 479, "w": 1, "h": 1})
    assert res.status_code == 201, res.text


# ---------------------------------------------------------------------------
# 5. cancel vs complete: exactly one terminal state
# ---------------------------------------------------------------------------

def test_cancel_queued_job_stays_canceled(client):
    image = fresh_image(client)
    set_flag("skip_all")  # worker holds off; job stays queued
    try:
        job = create_job(client, image["id"], {"x": 0, "y": 0, "w": 100, "h": 100})["job"]
        time.sleep(0.6)  # worker had chances to (not) claim it
        assert get_job(client, job["id"])["status"] == "queued"

        res = client.post(f"/api/jobs/{job['id']}/cancel")
        assert res.status_code == 200, res.text
        assert res.json()["job"]["status"] == "canceled"
    finally:
        clear_flag("skip_all")

    # Worker must never pick it up; state stays canceled, nothing publishes.
    time.sleep(2.0)
    job = get_job(client, job["id"])
    assert job["status"] == "canceled"
    assert job["published"] is False
    assert get_image(client, image["id"])["published"] is None

    # Second cancel attempt: already terminal.
    res = client.post(f"/api/jobs/{job['id']}/cancel")
    assert res.status_code == 409, res.text


def test_cancel_while_processing_loses_to_nothing(client):
    """Cancel lands mid-processing: the worker's late publish must be discarded."""
    image = fresh_image(client)
    set_flag("pause_mid_all")  # worker pauses between the two tmp files
    try:
        job = create_job(client, image["id"], {"x": 0, "y": 0, "w": 200, "h": 150})["job"]
        wait_job_status(client, job["id"], {"processing"})
        # Wait until the worker is definitely mid-render (first tmp written).
        wait_until(
            lambda: any(f"job-{job['id']}-" in n for n in ls(TMP_DIR)),
            desc="worker mid-processing tmp file",
        )
        res = client.post(f"/api/jobs/{job['id']}/cancel")
        assert res.status_code == 200, res.text
        assert res.json()["job"]["status"] == "canceled"
    finally:
        clear_flag("pause_mid_all")

    # The worker wakes up, finishes rendering, but its publish must lose and
    # its files must be discarded.
    wait_until(
        lambda: not any(n.startswith(f"job-{job['id']}-") for n in ls(TMP_DIR))
        and not any(n.startswith(f"job-{job['id']}-") for n in ls(DERIVATIVES_DIR)),
        desc="canceled job output discarded",
    )
    job = get_job(client, job["id"])
    assert job["status"] == "canceled"
    assert get_image(client, image["id"])["published"] is None


def test_cancel_after_succeed_is_rejected(client):
    image = fresh_image(client)
    job = create_job(client, image["id"], {"x": 0, "y": 0, "w": 120, "h": 90})["job"]
    wait_job_status(client, job["id"], {"succeeded"})
    res = client.post(f"/api/jobs/{job['id']}/cancel")
    assert res.status_code == 409, res.text
    assert get_job(client, job["id"])["status"] == "succeeded"


def test_cancel_complete_race_single_terminal_state(client):
    """Hammer create+immediate-cancel; the outcome must be one stable terminal state."""
    image = fresh_image(client)
    for i in range(8):
        # Fresh crop each round so every iteration gets its own job row.
        crop = {"x": 0, "y": 0, "w": 64 + i, "h": 64}
        job = create_job(client, image["id"], crop)["job"]
        cancel = client.post(f"/api/jobs/{job['id']}/cancel")
        assert cancel.status_code in (200, 409), cancel.text

        final = wait_job_status(client, job["id"], TERMINAL)
        if cancel.status_code == 200:
            assert final["status"] == "canceled"
        else:
            assert final["status"] == "succeeded"

        # Stability: the terminal state never flips afterwards.
        time.sleep(0.5)
        still = get_job(client, job["id"])
        assert still["status"] == final["status"]
        assert still["finished_at"] == final["finished_at"]


# ---------------------------------------------------------------------------
# 6. crash/restart recovery
# ---------------------------------------------------------------------------

def _docker_client():
    try:
        import docker
        return docker.from_env()
    except Exception:
        return None


def test_restart_recovery(client):
    docker_client = _docker_client()
    if docker_client is None:
        pytest.skip("docker socket not available")
    workers = docker_client.containers.list(
        filters={"label": "com.docker.compose.service=worker"})
    if not workers:
        pytest.skip("worker container not found")
    worker = workers[0]

    image = fresh_image(client)
    set_flag("pause_mid_all")  # worker will freeze mid-render
    job = create_job(client, image["id"], {"x": 0, "y": 0, "w": 300, "h": 200})["job"]
    try:
        wait_job_status(client, job["id"], {"processing"})
        tmp_name = f"job-{job['id']}-256.webp.tmp"
        wait_until(lambda: tmp_name in ls(TMP_DIR),
                   desc="first tmp derivative on disk")

        # Plant a stale tmp file to prove startup cleanup runs.
        junk = os.path.join(TMP_DIR, "stale-junk-from-crash.tmp")
        with open(junk, "w") as fh:
            fh.write("junk")

        # Hard-bounce the worker while the job is mid-processing.
        worker.restart(timeout=1)

        # Recovery: only the worker's startup cleanup can remove the junk
        # file, so its disappearance proves recovery ran.
        wait_until(lambda: not os.path.exists(junk), timeout=60,
                   desc="tmp cleanup after restart")
    finally:
        clear_flag("pause_mid_all")

    # ...and the interrupted job is retried to completion.
    final = wait_job_status(client, job["id"], {"succeeded"}, timeout=60)
    assert final["attempts"] >= 2, f"expected a retry, attempts={final['attempts']}"

    published = get_image(client, image["id"])["published"]
    assert published["id"] == job["id"]
    assert_derivative(client, published["derivative_urls"]["256"], 256)
    assert_derivative(client, published["derivative_urls"]["512"], 512)


# ---------------------------------------------------------------------------
# 7. a late-finishing stale job must not overwrite the newer published version
# ---------------------------------------------------------------------------

def test_stale_job_never_overwrites_newer_published(client):
    image = fresh_image(client)

    # Queue two jobs while the worker is held off, then pin the older one
    # with its own skip flag BEFORE releasing the worker, so the newer job
    # is guaranteed to publish first.
    set_flag("skip_all")
    try:
        job_a = create_job(client, image["id"], {"x": 0, "y": 0, "w": 200, "h": 200})["job"]
        job_b = create_job(client, image["id"], {"x": 50, "y": 50, "w": 300, "h": 300})["job"]
        assert job_a["version"] == 1 and job_b["version"] == 2
        set_flag(f"skip_job_{job_a['id']}")
    finally:
        clear_flag("skip_all")

    try:
        wait_job_status(client, job_b["id"], {"succeeded"})
        wait_until(
            lambda: (get_image(client, image["id"])["published"] or {}).get("id") == job_b["id"],
            desc="job B published")
        assert get_job(client, job_a["id"])["status"] == "queued"  # still held back
    finally:
        clear_flag(f"skip_job_{job_a['id']}")

    # The stale job now completes *after* the newer one was published.
    wait_job_status(client, job_a["id"], {"succeeded"})
    time.sleep(1.0)  # give any erroneous write-back a chance to happen

    image = get_image(client, image["id"])
    assert image["published"]["id"] == job_b["id"], "stale job overwrote the published version"
    assert image["published_version"] == 2
    job_a_state = get_job(client, job_a["id"])
    assert job_a_state["status"] == "succeeded"
    assert job_a_state["published"] is False
    # The served derivatives are still job B's files.
    assert_derivative(client, image["published"]["derivative_urls"]["256"], 256)
