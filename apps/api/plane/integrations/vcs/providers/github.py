# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import hmac
from typing import Any

from plane.integrations.vcs.events import PingEvent, PullRequestEvent
from plane.integrations.vcs.providers.base import VcsProvider


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class GitHubProvider(VcsProvider):
    key: str = "github"

    def verify(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        signature = _get_header(headers, "X-Hub-Signature-256")
        if not signature or not signature.startswith("sha256="):
            return False
        expected_hex = signature[len("sha256=") :]
        computed_hex = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected_hex.encode("utf-8"), computed_hex.encode("utf-8"))

    def delivery_id(self, headers: Mapping[str, str]) -> str | None:
        return _get_header(headers, "X-GitHub-Delivery")

    def event_name(self, headers: Mapping[str, str]) -> str:
        return _get_header(headers, "X-GitHub-Event") or ""

    def parse(self, headers: Mapping[str, str], payload: dict[str, Any]) -> PullRequestEvent | PingEvent | None:
        event = self.event_name(headers)
        if event == "ping":
            return PingEvent(provider=self.key)
        if event != "pull_request":
            return None

        pr = payload.get("pull_request")
        if not isinstance(pr, dict):
            return None

        action = payload.get("action", "")
        is_merged = bool(pr.get("merged", False))
        if action == "closed" and is_merged:
            action = "merged"

        body = pr.get("body")
        if body is None:
            body = ""
        elif len(body) > 65536:
            body = body[:65536]

        repo = payload.get("repository") or {}
        repo_full_name = repo.get("full_name", "")
        number = pr.get("number") or payload.get("number") or 0
        url = pr.get("html_url", "")
        title = pr.get("title", "")
        source_branch = pr.get("head", {}).get("ref", "")
        base_branch = pr.get("base", {}).get("ref", "")
        is_draft = bool(pr.get("draft", False))
        author_login = pr.get("user", {}).get("login", "")
        raw_updated_at = pr.get("updated_at")
        if not raw_updated_at:
            raise ValueError("missing updated_at")
        updated_at = _parse_iso_datetime(raw_updated_at)
        if updated_at is None:
            raise ValueError("missing updated_at")
        merged_at = _parse_iso_datetime(pr.get("merged_at"))

        return PullRequestEvent(
            provider=self.key,
            repo_full_name=repo_full_name,
            number=number,
            url=url,
            title=title,
            body=body,
            source_branch=source_branch,
            base_branch=base_branch,
            action=action,
            is_draft=is_draft,
            author_login=author_login,
            updated_at=updated_at,
            merged_at=merged_at,
        )

    def trim_payload(self, headers: Mapping[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        event = self.event_name(headers)
        if event != "pull_request":
            return {}

        pr = payload.get("pull_request", {})
        user = pr.get("user", {})
        head = pr.get("head", {})
        base = pr.get("base", {})
        repo = payload.get("repository", {})

        trimmed_user: dict[str, Any] = {}
        if "login" in user:
            trimmed_user["login"] = user["login"]

        trimmed_head: dict[str, Any] = {}
        if "ref" in head:
            trimmed_head["ref"] = head["ref"]

        trimmed_base: dict[str, Any] = {}
        if "ref" in base:
            trimmed_base["ref"] = base["ref"]

        trimmed_pr: dict[str, Any] = {}
        for k in ("number", "title", "body", "html_url", "draft", "merged", "merged_at", "updated_at"):
            if k in pr:
                val = pr[k]
                if k == "body" and isinstance(val, str) and len(val) > 65536:
                    val = val[:65536]
                trimmed_pr[k] = val
        if trimmed_user:
            trimmed_pr["user"] = trimmed_user
        if trimmed_head:
            trimmed_pr["head"] = trimmed_head
        if trimmed_base:
            trimmed_pr["base"] = trimmed_base

        trimmed_repo: dict[str, Any] = {}
        if "full_name" in repo:
            trimmed_repo["full_name"] = repo["full_name"]

        trimmed: dict[str, Any] = {}
        if "action" in payload:
            trimmed["action"] = payload["action"]
        if "number" in payload:
            trimmed["number"] = payload["number"]
        if trimmed_pr:
            trimmed["pull_request"] = trimmed_pr
        if trimmed_repo:
            trimmed["repository"] = trimmed_repo

        return trimmed
