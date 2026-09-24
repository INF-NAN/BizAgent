"""Request / response models for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateSession(BaseModel):
    scenario: str
    allowlist: list[str] | None = Field(default=None, description="tool names; null = all discovered now")


class SessionOut(BaseModel):
    session_id: str
    scenario: str
    env_url: str
    thread_id: str
    tools: list[dict[str, Any]]


class MessageIn(BaseModel):
    content: str = Field(min_length=1)
    user_id: str = "default"


class ApprovalIn(BaseModel):
    approved: bool
    approver: str = Field(min_length=1)
    reason: str = ""
