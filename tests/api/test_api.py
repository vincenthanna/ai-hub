"""HTTP contract tests."""

from __future__ import annotations

import hashlib
import io
import json
import uuid

import pytest


def mk(**over):
    payload = {
        "from": "backend-work",
        "title": "OAuth refresh 토큰 만료 처리 누락",
        "body": "재현 절차는 다음과 같다. 만료된 refresh token 으로 401 루프가 발생한다.",
        "tags": ["auth", "bug"],
        "kind": "handoff",
        "client_msg_id": str(uuid.uuid4()),
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------- auth
def test_health_needs_no_token(client):
    client.headers.pop("X-AIHub-Token", None)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["auth_enabled"] is True


def test_missing_token_is_401_with_envelope(client):
    client.headers.pop("X-AIHub-Token", None)
    r = client.post("/v1/items", json=mk())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert r.json()["error"]["request_id"]


def test_wrong_token_is_401(client):
    client.headers["X-AIHub-Token"] = "nope"
    assert client.get("/v1/items").status_code == 401


def test_bearer_token_also_accepted(client):
    client.headers.pop("X-AIHub-Token", None)
    r = client.get("/v1/items", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200


# ---------------------------------------------------------------- upload
def test_upload_returns_201_and_is_fast(client):
    r = client.post("/v1/items", json=mk())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["item_id"].startswith("01")
    assert body["seq"] >= 1
    assert body["deduplicated"] is False
    assert body["classification"]["status"] == "pending"


def test_idempotent_upload(client):
    payload = mk()
    a = client.post("/v1/items", json=payload)
    b = client.post("/v1/items", json=payload)
    assert a.status_code == 201 and b.status_code == 200
    assert b.json()["deduplicated"] is True
    assert a.json()["item_id"] == b.json()["item_id"]
    assert len(client.get("/v1/items").json()["items"]) == 1


def test_same_client_msg_id_different_body_conflicts(client):
    payload = mk()
    client.post("/v1/items", json=payload)
    r = client.post("/v1/items", json={**payload, "body": "완전히 다른 본문"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_oversized_body_rejected(client, config):
    big = "x" * (config.max_body_bytes + 10)
    r = client.post("/v1/items", json=mk(body=big))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


def test_validation_error_envelope(client):
    r = client.post("/v1/items", json={"title": "no sender"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


def test_bad_label_is_rejected(client):
    r = client.post("/v1/items", json=mk(**{"from": "!!!"}))
    assert r.status_code == 422


# ---------------------------------------------------------------- retrieval
def test_detail_returns_full_body(client):
    up = client.post("/v1/items", json=mk()).json()
    r = client.get("/v1/items/%s" % up["item_id"])
    assert r.status_code == 200
    d = r.json()
    assert "401 루프" in d["body"]
    assert d["tags"] == ["auth", "bug"]
    assert d["sender"] == "backend-work"


def test_unknown_item_is_404(client):
    r = client.get("/v1/items/01ZZZZZZZZZZZZZZZZZZZZZZZZ")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_list_preview_is_truncated(client):
    client.post("/v1/items", json=mk(body="가" * 5000))
    item = client.get("/v1/items").json()["items"][0]
    assert item["body_truncated"] is True
    assert len(item["body_preview"]) <= 401


def test_cursor_pagination_has_no_gaps_or_repeats(client):
    for i in range(25):
        client.post("/v1/items", json=mk(title="아이템 %d" % i, body="본문 %d" % i))
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 7}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/items", params=params).json()
        seen.extend(i["item_id"] for i in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 25
    assert len(set(seen)) == 25


def test_bad_cursor_is_400(client):
    r = client.get("/v1/items", params={"cursor": "@@@@"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------- routing
def test_direct_message_reaches_only_the_addressee(client):
    client.get("/v1/inbox", params={"as": "frontend-app"})
    client.get("/v1/inbox", params={"as": "other-session"})
    up = client.post("/v1/items", json=mk(to=["frontend-app"])).json()

    mine = client.get("/v1/inbox", params={"as": "frontend-app"}).json()
    assert [i["item_id"] for i in mine["items"]] == [up["item_id"]]
    assert mine["items"][0]["delivery_kind"] == "direct"

    other = client.get("/v1/inbox", params={"as": "other-session"}).json()
    assert up["item_id"] not in [i["item_id"] for i in other["items"]]


def test_broadcast_reaches_everyone_registered_before_it(client):
    client.get("/v1/inbox", params={"as": "watcher"})
    up = client.post("/v1/items", json=mk(to=None)).json()
    inbox = client.get("/v1/inbox", params={"as": "watcher"}).json()
    assert [i["item_id"] for i in inbox["items"]] == [up["item_id"]]
    assert inbox["items"][0]["delivery_kind"] == "broadcast"


def test_new_label_does_not_inherit_old_broadcasts(client):
    client.post("/v1/items", json=mk(to=None))
    inbox = client.get("/v1/inbox", params={"as": "brand-new"}).json()
    assert inbox["items"] == []
    assert inbox["unseen_broadcast_count"] == 0


def test_sender_does_not_receive_own_broadcast(client):
    client.post("/v1/items", json=mk(**{"from": "solo"}, to=None))
    assert client.get("/v1/inbox", params={"as": "solo"}).json()["items"] == []


def test_polling_does_not_mark_read(client):
    client.get("/v1/inbox", params={"as": "frontend-app"})
    client.post("/v1/items", json=mk(to=["frontend-app"]))
    for _ in range(3):
        got = client.get("/v1/inbox", params={"as": "frontend-app"}).json()
        assert got["pending_direct_count"] == 1


def test_ack_clears_and_is_idempotent(client):
    client.get("/v1/inbox", params={"as": "frontend-app"})
    up = client.post("/v1/items", json=mk(to=["frontend-app"])).json()
    a = client.post(
        "/v1/inbox/ack", json={"as": "frontend-app", "item_ids": [up["item_id"]]}
    ).json()
    assert a["acked"] == 1 and a["pending_direct_count"] == 0
    b = client.post(
        "/v1/inbox/ack", json={"as": "frontend-app", "item_ids": [up["item_id"]]}
    ).json()
    assert b["acked"] == 0 and b["already_acked"] == 1
    assert client.get("/v1/inbox", params={"as": "frontend-app"}).json()["items"] == []


def test_ack_all_clears_direct_and_broadcast(client):
    client.get("/v1/inbox", params={"as": "reader"})
    client.post("/v1/items", json=mk(to=["reader"]))
    client.post("/v1/items", json=mk(to=None))
    assert len(client.get("/v1/inbox", params={"as": "reader"}).json()["items"]) == 2
    client.post("/v1/inbox/ack", json={"as": "reader", "all": True})
    assert client.get("/v1/inbox", params={"as": "reader"}).json()["items"] == []


def test_empty_ack_is_400(client):
    r = client.post("/v1/inbox/ack", json={"as": "reader"})
    assert r.status_code == 400


def test_agents_listing(client):
    client.post("/v1/items", json=mk(to=["frontend-app"]))
    labels = {a["label"] for a in client.get("/v1/agents").json()["agents"]}
    assert {"backend-work", "frontend-app"} <= labels


# ---------------------------------------------------------------- search
def test_korean_partial_search(client):
    client.post("/v1/items", json=mk(title="메모리누수 조사", body="uv PATH 설정 문제"))
    hits = client.get("/v1/search", params={"q": "메모리"}).json()["items"]
    assert len(hits) == 1
    assert "메모리누수" in hits[0]["title"]


def test_search_snippet_and_score(client):
    client.post("/v1/items", json=mk(body="connection reset by peer 가 반복된다"))
    r = client.get("/v1/search", params={"q": "connection reset"}).json()
    assert r["items"], r
    assert "snippet" in r["items"][0]
    assert r["items"][0]["score"] > 0
    assert r["took_ms"] >= 0


def test_search_filters(client):
    client.post("/v1/items", json=mk(**{"from": "alpha"}, title="배포 실패", topic="infra"))
    client.post("/v1/items", json=mk(**{"from": "beta"}, title="배포 성공", topic="infra"))
    only_alpha = client.get("/v1/search", params={"q": "배포", "from": "alpha"}).json()
    assert [i["sender"] for i in only_alpha["items"]] == ["alpha"]


def test_search_with_empty_query_returns_recent(client):
    client.post("/v1/items", json=mk())
    r = client.get("/v1/search", params={"q": ""}).json()
    assert len(r["items"]) == 1


def test_search_injection_is_harmless(client):
    client.post("/v1/items", json=mk())
    r = client.get("/v1/search", params={"q": 'a" OR 1=1 --'})
    assert r.status_code == 200


def test_topics_endpoint(client):
    client.post("/v1/items", json=mk(topic="infra"))
    topics = {t["topic"]: t["count"] for t in client.get("/v1/topics").json()["topics"]}
    assert topics.get("infra") == 1
    assert "unsorted" in topics


# ---------------------------------------------------------------- attachments
def test_multipart_upload_and_download_roundtrip(client):
    content = b"stack trace line 1\nline 2\n" * 100
    payload = json.dumps(mk(title="첨부 포함"))
    r = client.post(
        "/v1/items",
        data={"payload": payload},
        files=[("files", ("trace.log", io.BytesIO(content), "text/plain"))],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["size_bytes"] == len(content)
    assert att["sha256"] == hashlib.sha256(content).hexdigest()

    got = client.get(att["download_url"])
    assert got.status_code == 200
    assert got.content == content
    assert got.headers["X-AIHub-Sha256"] == att["sha256"]


def test_identical_attachments_share_one_blob(client, config):
    content = b"same bytes"
    for _ in range(2):
        client.post(
            "/v1/items",
            data={"payload": json.dumps(mk())},
            files=[("files", ("a.txt", io.BytesIO(content), "text/plain"))],
        )
    blobs = [p for p in (config.blobs_dir).rglob("*") if p.is_file() and "tmp" not in p.parts]
    assert len(blobs) == 1


def test_oversized_attachment_rejected(client, config):
    big = b"x" * (config.max_file_bytes + 1024)
    r = client.post(
        "/v1/items",
        data={"payload": json.dumps(mk())},
        files=[("files", ("big.bin", io.BytesIO(big), "application/octet-stream"))],
    )
    assert r.status_code == 413


def test_large_body_goes_to_blob_and_round_trips(client):
    big = "가나다라" * 100_000  # ~1.2 MB utf-8, above the 256 KB DB threshold
    r = client.post("/v1/items", json=mk(body=big[: 250_000]))
    assert r.status_code == 201
    detail = client.get("/v1/items/%s" % r.json()["item_id"]).json()
    assert detail["body"] == big[: 250_000]
