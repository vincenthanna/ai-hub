"""Classification worker behaviour."""

from __future__ import annotations

import time
import uuid

import pytest


def mk(**over):
    payload = {
        "from": "backend-work",
        "title": "ds30 배포 실패",
        "body": "systemd 로 서버를 재시작했는데 uv 경로가 PATH 에 없어 포트 바인딩에 실패한다.",
        "client_msg_id": str(uuid.uuid4()),
    }
    payload.update(over)
    return payload


@pytest.fixture()
def hclient(config):
    """Client with classification on, but the claude CLI deliberately absent."""
    from fastapi.testclient import TestClient

    from aihub.app import create_app

    config.classify.enabled = True
    config.classify.claude_bin = "/nonexistent/claude-binary"
    config.classify.batch_wait_sec = 0.0
    app = create_app(config)
    with TestClient(app) as c:
        c.headers.update({"X-AIHub-Token": "test-token"})
        yield c


def wait_for(client, item_id, states=("auto", "heuristic", "failed"), timeout=15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get("/v1/items/%s" % item_id).json()
        if last["classification"] in states:
            return last
        time.sleep(0.2)
    return last


def test_missing_claude_falls_back_to_rules(hclient):
    up = hclient.post("/v1/items", json=mk()).json()
    assert up["classification"]["status"] == "pending"
    got = wait_for(hclient, up["item_id"])
    assert got["classification"] == "heuristic", got
    assert got["topic"] == "infra-deploy"
    assert "infra-deploy" in got["tags"]
    assert got["summary"]


def test_health_reports_heuristic_engine(hclient):
    assert hclient.get("/health").json()["classifier"]["engine"] == "heuristic"


def test_upload_stays_fast_while_classifying(hclient):
    started = time.perf_counter()
    for _ in range(10):
        r = hclient.post("/v1/items", json=mk())
        assert r.status_code == 201
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, "10 uploads took %.2fs; the request path is blocking" % elapsed


def test_classified_item_becomes_searchable_by_new_tag(hclient):
    up = hclient.post("/v1/items", json=mk()).json()
    wait_for(hclient, up["item_id"])
    hits = hclient.get("/v1/search", params={"q": "infra-deploy"}).json()["items"]
    assert up["item_id"] in [h["item_id"] for h in hits]


def test_manual_topic_is_not_overwritten(hclient):
    up = hclient.post("/v1/items", json=mk(topic="my-own-topic")).json()
    detail = hclient.get("/v1/items/%s" % up["item_id"]).json()
    assert detail["topic"] == "my-own-topic"
    assert detail["classification"] == "manual"
    time.sleep(1.0)
    again = hclient.get("/v1/items/%s" % up["item_id"]).json()
    assert again["topic"] == "my-own-topic", "auto classification overwrote a manual topic"


def test_topic_resolution_rules(config):
    """The server, not the model, enforces topic hygiene."""
    from aihub.classify.worker import ClassifyWorker

    class FakeApp:
        class state:
            pass

    from aihub.storage.blobs import BlobStore
    from aihub.storage.db import Database
    from aihub.storage.migrate import migrate
    from aihub.storage.repo import Repo

    db = Database(config.db_path)
    migrate(db.writer)
    app = FakeApp()
    app.state.config = config
    app.state.db = db
    app.state.repo = Repo(db, BlobStore(config.blobs_dir))
    w = ClassifyWorker(app)
    known = ["infra-deploy", "auth-security"]

    # a near-duplicate is absorbed
    assert w._resolve_topic({"topic_id": "infra-deplo", "topic_action": "new",
                             "topic_confidence": 0.99}, known) == "infra-deploy"
    # low confidence never creates a topic
    assert w._resolve_topic({"topic_id": "brand-new-thing", "topic_action": "new",
                             "topic_confidence": 0.4}, known) == "unsorted"
    # garbage falls through to unsorted
    assert w._resolve_topic({"topic_id": "!!!", "topic_action": "new",
                             "topic_confidence": 0.9}, known) == "unsorted"
    # an existing topic passes straight through
    assert w._resolve_topic({"topic_id": "auth-security"}, known) == "auth-security"
    # the daily budget caps new topics
    created = [
        w._resolve_topic(
            {"topic_id": "zzz-topic-%d" % i, "topic_action": "new", "topic_confidence": 0.95},
            known,
        )
        for i in range(6)
    ]
    assert created.count("unsorted") >= 3, created
    db.close()


def test_batch_result_mismatch_does_not_cross_assign(config, monkeypatch):
    """A model that returns fewer or reordered results must not mislabel items."""
    import asyncio

    from aihub.classify import worker as worker_mod
    from aihub.storage.blobs import BlobStore
    from aihub.storage.db import Database
    from aihub.storage.migrate import migrate
    from aihub.storage.repo import Repo

    db = Database(config.db_path)
    migrate(db.writer)
    repo = Repo(db, BlobStore(config.blobs_dir))

    class FakeApp:
        class state:
            pass

    app = FakeApp()
    app.state.config = config
    app.state.db = db
    app.state.repo = repo
    w = worker_mod.ClassifyWorker(app)
    w.engine = "claude"
    w._seed_topics()  # start() normally does this

    a, _ = repo.create_item(sender="s", to=[], kind="note", title="첫번째 배포 이슈",
                            body="systemd uv PATH 포트", topic=None, tags=[])
    b, _ = repo.create_item(sender="s", to=[], kind="note", title="두번째 인증 이슈",
                            body="oauth 토큰 만료 로그인", topic=None, tags=[])

    # Only ref 1 comes back, and it names a topic that belongs to item b.
    async def fake_batch(items, topics, tags, **kw):
        return [{"ref": 1, "topic_id": "auth-security", "topic_action": "existing",
                 "topic_confidence": 0.9, "tags": ["oauth"], "summary": "인증 이슈",
                 "importance": 3, "kind": "issue"}]

    monkeypatch.setattr(worker_mod, "classify_batch", fake_batch)
    jobs = [
        {"job_id": "j1", "item_id": a["item_id"], "attempt": 0, "max_attempts": 2,
         "title": a["title"], "body": "systemd uv PATH 포트"},
        {"job_id": "j2", "item_id": b["item_id"], "attempt": 0, "max_attempts": 2,
         "title": b["title"], "body": "oauth 토큰 만료 로그인"},
    ]
    for job in jobs:
        db.writer.execute(
            "INSERT INTO classification_jobs(job_id,item_id,input_hash,state,next_run_ms,created_ms)"
            " VALUES(?,?,?, 'running', 0, 0)",
            (job["job_id"], job["item_id"], job["job_id"]),
        )
    asyncio.run(w._process(jobs))

    got_a = repo.get_item(a["item_id"])
    got_b = repo.get_item(b["item_id"])
    assert got_b["topic"] == "auth-security", got_b
    assert got_b["classification"] == "auto"
    # item a had no result, so it must be classified by rules, not by b's answer
    assert got_a["classification"] == "heuristic", got_a
    assert got_a["topic"] != "auth-security"
    db.close()
