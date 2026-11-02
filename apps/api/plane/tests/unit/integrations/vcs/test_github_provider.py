# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from plane.integrations.vcs.events import PingEvent, PullRequestEvent
from plane.integrations.vcs.providers import registry
from plane.integrations.vcs.providers.base import VcsProvider
from plane.integrations.vcs.providers.github import GitHubProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "github"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _sign_sha256(raw_body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.mark.unit
def test_verify_accepts_valid_signature():
    provider = GitHubProvider()
    raw_body = (FIXTURES_DIR / "opened.json").read_bytes()
    secret = "whsec_test_secret_123"
    headers = {"X-Hub-Signature-256": _sign_sha256(raw_body, secret)}

    assert provider.verify(headers, raw_body, secret) is True


@pytest.mark.unit
def test_verify_rejects_wrong_secret():
    provider = GitHubProvider()
    raw_body = (FIXTURES_DIR / "opened.json").read_bytes()
    headers = {"X-Hub-Signature-256": _sign_sha256(raw_body, "correct-secret")}

    assert provider.verify(headers, raw_body, "wrong-secret") is False
    assert provider.verify(headers, raw_body + b" ", "correct-secret") is False


@pytest.mark.unit
def test_verify_rejects_missing_header():
    provider = GitHubProvider()
    raw_body = b'{"action": "opened"}'

    assert provider.verify({}, raw_body, "secret") is False
    assert provider.verify({"X-Hub-Signature-256": ""}, raw_body, "secret") is False


@pytest.mark.unit
def test_verify_rejects_sha1_header():
    provider = GitHubProvider()
    raw_body = b'{"action": "opened"}'
    secret = "whsec_test_secret_123"
    sha1_hex = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha1).hexdigest()
    sha256_hex = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    assert provider.verify({"X-Hub-Signature": f"sha1={sha1_hex}"}, raw_body, secret) is False
    assert provider.verify({"X-Hub-Signature-256": f"sha1={sha1_hex}"}, raw_body, secret) is False
    assert provider.verify({"X-Hub-Signature-256": f"sha1={sha256_hex}"}, raw_body, secret) is False


@pytest.mark.unit
def test_parse_opened():
    provider = GitHubProvider()
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-opened-001",
    }
    payload = _load_fixture("opened")

    assert provider.delivery_id(headers) == "delivery-opened-001"
    assert provider.delivery_id({}) is None
    assert provider.event_name(headers) == "pull_request"

    event = provider.parse(headers, payload)

    assert isinstance(event, PullRequestEvent)
    assert is_dataclass(event)
    with pytest.raises(FrozenInstanceError):
        event.action = "closed"  # type: ignore[misc]

    assert event == PullRequestEvent(
        provider="github",
        repo_full_name="Codeavers/plane",
        number=42,
        url="https://github.com/Codeavers/plane/pull/42",
        title="feat(vcs): CODEA-40 add GitHub webhook sync",
        body="Fixes CODEA-40\n\nSyncs pull request events.",
        source_branch="feat/CODEA-40-github-sync",
        base_branch="develop",
        action="opened",
        is_draft=False,
        author_login="octocat",
        updated_at=datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc),
        merged_at=None,
    )

    for fixture_name, expected_action in (
        ("ready_for_review", "ready_for_review"),
        ("reopened", "reopened"),
        ("edited", "edited"),
        ("synchronize", "synchronize"),
    ):
        other = provider.parse(headers, _load_fixture(fixture_name))
        assert isinstance(other, PullRequestEvent)
        assert other.action == expected_action


@pytest.mark.unit
def test_parse_draft_flag():
    provider = GitHubProvider()
    headers = {"X-GitHub-Event": "pull_request"}

    draft_event = provider.parse(headers, _load_fixture("opened_draft"))
    assert isinstance(draft_event, PullRequestEvent)
    assert draft_event.action == "opened"
    assert draft_event.is_draft is True

    ready_event = provider.parse(headers, _load_fixture("ready_for_review"))
    assert isinstance(ready_event, PullRequestEvent)
    assert ready_event.action == "ready_for_review"
    assert ready_event.is_draft is False


@pytest.mark.unit
def test_parse_closed_merged_maps_to_merged():
    provider = GitHubProvider()
    headers = {"X-GitHub-Event": "pull_request"}

    event = provider.parse(headers, _load_fixture("closed_merged"))

    assert isinstance(event, PullRequestEvent)
    assert event.action == "merged"
    assert event.merged_at == datetime(2026, 10, 29, 12, 30, 0, tzinfo=timezone.utc)
    assert event.updated_at == datetime(2026, 10, 29, 12, 30, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_parse_closed_unmerged_stays_closed():
    provider = GitHubProvider()
    headers = {"X-GitHub-Event": "pull_request"}

    event = provider.parse(headers, _load_fixture("closed_unmerged"))

    assert isinstance(event, PullRequestEvent)
    assert event.action == "closed"
    assert event.merged_at is None


@pytest.mark.unit
def test_parse_ping():
    provider = GitHubProvider()
    headers = {"X-GitHub-Event": "ping"}

    event = provider.parse(headers, _load_fixture("ping"))

    assert event == PingEvent(provider="github")
    assert is_dataclass(event)
    with pytest.raises(FrozenInstanceError):
        event.provider = "gitlab"  # type: ignore[misc]


@pytest.mark.unit
def test_parse_ignores_other_events():
    provider = GitHubProvider()
    payload = _load_fixture("issues_event")

    assert provider.parse({"X-GitHub-Event": "issues"}, payload) is None
    assert provider.parse({"X-GitHub-Event": "push"}, {}) is None


@pytest.mark.unit
def test_parse_caps_body_length():
    provider = GitHubProvider()
    headers = {"X-GitHub-Event": "pull_request"}

    oversized_payload = copy.deepcopy(_load_fixture("opened"))
    oversized_payload["pull_request"]["body"] = "x" * 70_000
    oversized_event = provider.parse(headers, oversized_payload)
    assert isinstance(oversized_event, PullRequestEvent)
    assert len(oversized_event.body) == 65_536
    assert oversized_event.body == "x" * 65_536

    null_body_payload = copy.deepcopy(_load_fixture("opened"))
    null_body_payload["pull_request"]["body"] = None
    null_body_event = provider.parse(headers, null_body_payload)
    assert isinstance(null_body_event, PullRequestEvent)
    assert null_body_event.body == ""


@pytest.mark.unit
def test_trim_payload_keeps_only_read_fields():
    provider = GitHubProvider()
    headers = {"X-GitHub-Event": "pull_request"}
    payload = _load_fixture("opened")

    trimmed = provider.trim_payload(headers, payload)

    assert "sender" not in trimmed
    assert "private" not in trimmed["repository"]
    assert "html_url" not in trimmed["repository"]
    assert "diff_url" not in trimmed["pull_request"]
    assert "node_id" not in trimmed["pull_request"]
    assert "commits" not in trimmed["pull_request"]
    assert "avatar_url" not in trimmed["pull_request"]["user"]
    assert "sha" not in trimmed["pull_request"]["head"]
    assert "sha" not in trimmed["pull_request"]["base"]

    # Stored trimmed payload must still parse into the exact same domain event.
    assert provider.parse(headers, trimmed) == provider.parse(headers, payload)


@pytest.mark.unit
def test_registry_returns_github():
    gh = registry.get("github")

    assert isinstance(gh, GitHubProvider)
    assert isinstance(gh, VcsProvider)
    assert gh.key == "github"
    assert any(p.key == "github" for p in registry.all())

    registry.register(GitHubProvider())
    assert registry.get("github").key == "github"

    with pytest.raises(KeyError):
        registry.get("nonexistent_provider")
