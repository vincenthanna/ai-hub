"""Request and response schemas."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")

KINDS = ("note", "message", "handoff", "issue", "decision", "artifact")
PRIORITIES = ("low", "normal", "high")


def normalize_label(value: str) -> str:
    """Fold a human-typed agent name into the canonical label form."""
    out = re.sub(r"[^a-z0-9._-]+", "-", (value or "").strip().lower())
    out = re.sub(r"-{2,}", "-", out).strip("-")
    if not out:
        raise ValueError("label is empty after normalization")
    if not LABEL_RE.match(out):
        raise ValueError("label must match %s" % LABEL_RE.pattern)
    return out


def normalize_tag(value: str) -> Optional[str]:
    out = re.sub(r"[^a-z0-9._-]+", "-", (value or "").strip().lower()).strip("-")
    return out if out and TAG_RE.match(out) else None


class Ref(BaseModel):
    type: str = Field(default="url", max_length=20)
    value: str = Field(max_length=1024)
    line: Optional[int] = None


class ItemCreate(BaseModel):
    sender: str = Field(alias="from", max_length=64)
    to: Optional[List[str]] = None
    kind: str = "note"
    title: str = Field(default="", max_length=200)
    body: str = ""
    topic: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    repo: str = Field(default="", max_length=200)
    host: str = Field(default="", max_length=100)
    ref: str = Field(default="", max_length=200)
    refs: List[Ref] = Field(default_factory=list)
    client_msg_id: Optional[str] = Field(default=None, max_length=100)
    priority: str = "normal"
    importance: Optional[int] = None

    model_config = {"populate_by_name": True}

    @field_validator("sender")
    @classmethod
    def _sender(cls, v: str) -> str:
        return normalize_label(v)

    @field_validator("to")
    @classmethod
    def _to(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if not v:
            return None
        seen: List[str] = []
        for item in v[:20]:
            label = normalize_label(item)
            if label not in seen:
                seen.append(label)
        return seen or None

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        return v if v in KINDS else "note"

    @field_validator("priority")
    @classmethod
    def _priority(cls, v: str) -> str:
        return v if v in PRIORITIES else "normal"

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: List[str]) -> List[str]:
        out: List[str] = []
        for tag in (v or [])[:20]:
            norm = normalize_tag(tag)
            if norm and norm not in out:
                out.append(norm)
        return out

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        norm = re.sub(r"[^a-z0-9-]+", "-", v.strip().lower()).strip("-")
        return norm if TOPIC_RE.match(norm) else None

    @field_validator("importance")
    @classmethod
    def _importance(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        return max(1, min(5, int(v)))


class AckRequest(BaseModel):
    recipient: str = Field(alias="as", max_length=64)
    item_ids: List[str] = Field(default_factory=list)
    broadcast_upto_seq: Optional[int] = None
    all: bool = False
    note: str = Field(default="", max_length=200)

    model_config = {"populate_by_name": True}

    @field_validator("recipient")
    @classmethod
    def _recipient(cls, v: str) -> str:
        return normalize_label(v)


class ReclassifyRequest(BaseModel):
    item_ids: List[str] = Field(default_factory=list)
    topic: Optional[str] = None
    status: Optional[str] = None
    limit: int = 100
    force: bool = False
    engine: Optional[str] = None


class AttachmentOut(BaseModel):
    attachment_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    download_url: str


class ItemSummary(BaseModel):
    item_id: str
    seq: int
    kind: str
    title: str
    summary: str
    body_preview: str = ""
    body_truncated: bool = False
    topic: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    sender: str
    to: List[str] = Field(default_factory=list)
    priority: str = "normal"
    importance: int = 3
    classification: str = "pending"
    attachment_count: int = 0
    created_at: str
    created_ms: int


class ItemDetail(ItemSummary):
    body: str = ""
    body_bytes: int = 0
    repo: str = ""
    host: str = ""
    ref: str = ""
    refs: List[Dict[str, Any]] = Field(default_factory=list)
    attachments: List[AttachmentOut] = Field(default_factory=list)
    delivery: List[Dict[str, Any]] = Field(default_factory=list)
    classified_at: Optional[str] = None
