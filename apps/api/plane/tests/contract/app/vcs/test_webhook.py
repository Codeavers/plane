# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.test import APIClient, APIRequestFactory

from plane.db.models import Issue, IssueComment, IssueSequence, State
from plane.db.models.vcs import VcsConnection, VcsDelivery
from plane.integrations.vcs.providers.github import GitHubProvider
from plane.integrations.vcs.settings import DEFAULT_SETTINGS
from plane.integrations.vcs.webhook import VcsWebhookEndpoint
from plane.tests.factories import ProjectFactory

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "unit" / "integrations" / "vcs" / "fixtures" / "github"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _sign_body(raw_body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _webhook_url(provider: str, connection_id) -> str:
    return f"/api/integrations/vcs/{provider}/{connection_id}/"


def _post_webhook(
    client: APIClient,
    url: str,
    raw_body: bytes,
    *,
    signature: str | None,
    delivery_id: str | None = None,
    event: str = "pull_request",
):
    headers: dict[str, str] = {"HTTP_X_GITHUB_EVENT": event}
    if signature is not None:
        headers["HTTP_X_HUB_SIGNATURE_256"] = signature
    if delivery_id is not None:
        headers["HTTP_X_GITHUB_DELIVERY"] = delivery_id
    return client.post(url, data=raw_body, content_type="application/json", **headers)


@pytest.fixture
def connection(workspace, create_bot_user):
    return VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="GitHub Webhook Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_valid_signature_queues_delivery(api_client, connection):
    # Pretty-printed fixture bytes differ from json.dumps(parsed), ensuring raw request.body is verified.
    raw_body = _fixture_bytes("opened.json")
    sig = _sign_body(raw_body, connection.secret)

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        response = _post_webhook(
            api_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=sig,
            delivery_id="gh-deliv-valid-001",
        )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json() == {"status": "queued"}

    delivery = VcsDelivery.objects.get(connection=connection, delivery_id="gh-deliv-valid-001")
    assert delivery.status == VcsDelivery.STATUS_RECEIVED
    assert delivery.event == "pull_request"
    expected_trimmed = GitHubProvider().trim_payload({"X-GitHub-Event": "pull_request"}, json.loads(raw_body))
    assert delivery.payload == expected_trimmed
    assert "sender" not in delivery.payload

    connection.refresh_from_db()
    assert connection.last_delivery_at is not None

    mock_delay.assert_called_once_with(str(delivery.id))


@pytest.mark.contract
@pytest.mark.django_db
def test_invalid_signature_returns_401_and_stores_nothing(api_client, connection):
    raw_body = _fixture_bytes("opened.json")
    bad_sig = _sign_body(raw_body, "wrong-secret")

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        resp_wrong = _post_webhook(
            api_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=bad_sig,
            delivery_id="gh-deliv-bad-001",
        )
        resp_missing = _post_webhook(
            api_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=None,
            delivery_id="gh-deliv-bad-002",
        )

    assert resp_wrong.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp_wrong.json() == {}
    assert resp_missing.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp_missing.json() == {}

    assert VcsDelivery.objects.filter(connection=connection).count() == 0
    connection.refresh_from_db()
    assert connection.last_delivery_at is None
    mock_delay.assert_not_called()


@pytest.mark.contract
@pytest.mark.django_db
def test_unknown_connection_returns_404(api_client):
    raw_body = _fixture_bytes("opened.json")
    sig = _sign_body(raw_body, "any-secret")

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        response = _post_webhook(
            api_client,
            _webhook_url("github", uuid4()),
            raw_body,
            signature=sig,
            delivery_id="gh-deliv-unknown-001",
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {}
    assert VcsDelivery.objects.count() == 0
    mock_delay.assert_not_called()


@pytest.mark.contract
@pytest.mark.django_db
def test_inactive_connection_returns_404(api_client, connection):
    connection.is_active = False
    connection.save()

    raw_body = _fixture_bytes("opened.json")
    sig = _sign_body(raw_body, connection.secret)

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        response = _post_webhook(
            api_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=sig,
            delivery_id="gh-deliv-inactive-001",
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {}
    assert VcsDelivery.objects.filter(connection=connection).count() == 0
    mock_delay.assert_not_called()


@pytest.mark.contract
@pytest.mark.django_db
def test_provider_mismatch_returns_404(api_client, workspace, connection, create_bot_user):
    gitlab_conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="gitlab",
        name="GitLab Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    raw_body = _fixture_bytes("opened.json")

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        resp_unregistered_url = _post_webhook(
            api_client,
            _webhook_url("gitlab", connection.id),
            raw_body,
            signature=_sign_body(raw_body, connection.secret),
            delivery_id="gh-deliv-mismatch-001",
        )
        resp_wrong_conn_provider = _post_webhook(
            api_client,
            _webhook_url("github", gitlab_conn.id),
            raw_body,
            signature=_sign_body(raw_body, gitlab_conn.secret),
            delivery_id="gh-deliv-mismatch-002",
        )

    assert resp_unregistered_url.status_code == status.HTTP_404_NOT_FOUND
    assert resp_unregistered_url.json() == {}
    assert resp_wrong_conn_provider.status_code == status.HTTP_404_NOT_FOUND
    assert resp_wrong_conn_provider.json() == {}
    assert VcsDelivery.objects.count() == 0
    mock_delay.assert_not_called()


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_duplicate_delivery_returns_200_without_requeue(api_client, connection):
    raw_body = _fixture_bytes("opened.json")
    sig = _sign_body(raw_body, connection.secret)

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        first = _post_webhook(
            api_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=sig,
            delivery_id="gh-deliv-dup-001",
        )
        second = _post_webhook(
            api_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=sig,
            delivery_id="gh-deliv-dup-001",
        )

    assert first.status_code == status.HTTP_202_ACCEPTED
    assert first.json() == {"status": "queued"}
    assert second.status_code == status.HTTP_200_OK
    assert second.json() == {"status": "duplicate"}

    assert VcsDelivery.objects.filter(connection=connection, delivery_id="gh-deliv-dup-001").count() == 1
    assert mock_delay.call_count == 1


@pytest.mark.contract
@pytest.mark.django_db
def test_missing_delivery_id_returns_400(api_client, connection):
    raw_body = _fixture_bytes("opened.json")
    sig = _sign_body(raw_body, connection.secret)

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        response = _post_webhook(
            api_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=sig,
            delivery_id=None,
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert VcsDelivery.objects.filter(connection=connection).count() == 0
    mock_delay.assert_not_called()


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_endpoint_needs_no_session_or_api_key(connection):
    assert list(VcsWebhookEndpoint.authentication_classes) == []
    assert AllowAny in VcsWebhookEndpoint.permission_classes

    req = APIRequestFactory().post(_webhook_url("github", connection.id))
    view_a = VcsWebhookEndpoint()
    view_a.kwargs = {"provider": "github", "connection_id": uuid4()}
    view_b = VcsWebhookEndpoint()
    view_b.kwargs = {"provider": "github", "connection_id": uuid4()}

    matched_throttle = None
    for throttle in view_a.get_throttles():
        if getattr(throttle, "scope", None) is None and hasattr(view_a, "throttle_scope"):
            throttle.scope = view_a.throttle_scope
        if getattr(throttle, "scope", None) == "vcs_webhook":
            matched_throttle = throttle
            break
    assert matched_throttle is not None
    assert matched_throttle.parse_rate(matched_throttle.get_rate()) == (600, 60)
    key_a = matched_throttle.get_cache_key(req, view_a)
    key_b = matched_throttle.get_cache_key(req, view_b)
    assert key_a and key_b and key_a != key_b

    external_client = APIClient(enforce_csrf_checks=True)
    raw_body = _fixture_bytes("opened.json")
    sig = _sign_body(raw_body, connection.secret)

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        post_resp = _post_webhook(
            external_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=sig,
            delivery_id="gh-deliv-unauth-001",
        )
        get_resp = external_client.get(_webhook_url("github", connection.id))

    assert post_resp.status_code == status.HTTP_202_ACCEPTED
    assert post_resp.json() == {"status": "queued"}
    assert get_resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    mock_delay.assert_called_once()


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_end_to_end_eager_opened_moves_work_item(api_client, workspace, connection, create_user, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True

    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    todo_state = State.objects.create(
        project=project,
        name="Todo",
        group="unstarted",
        color="#60646C",
        default=True,
        sequence=15000,
        created_by=create_user,
    )
    in_review_state = State.objects.create(
        project=project,
        name="In Review",
        group="started",
        color="#3B82F6",
        default=False,
        sequence=35000,
        created_by=create_user,
    )
    IssueSequence.objects.create(project=project, workspace=workspace, sequence=39)
    issue_40 = Issue(name="Implement webhook endpoint", workspace=workspace, project=project, state=todo_state)
    issue_40.save(created_by_id=create_user.id)
    assert issue_40.sequence_id == 40

    raw_body = _fixture_bytes("opened.json")
    sig = _sign_body(raw_body, connection.secret)

    with patch("plane.integrations.vcs.actions.issue_activity"):
        response = _post_webhook(
            api_client,
            _webhook_url("github", connection.id),
            raw_body,
            signature=sig,
            delivery_id="gh-deliv-e2e-001",
        )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json() == {"status": "queued"}

    issue_40.refresh_from_db()
    assert issue_40.state_id == in_review_state.id
    assert IssueComment.objects.filter(issue=issue_40, external_source="vcs").count() == 1

    delivery = VcsDelivery.objects.get(connection=connection, delivery_id="gh-deliv-e2e-001")
    assert delivery.status == VcsDelivery.STATUS_PROCESSED


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_invalid_json_or_non_object_body_returns_400(api_client, connection):
    bad_bodies = [
        b"{not valid json",
        b"",
        b"[1, 2, 3]",
        b'"plain string"',
        b"null",
        b"\xff\xfe",
    ]

    with patch("plane.integrations.vcs.webhook.process_vcs_delivery.delay") as mock_delay:
        for idx, raw_body in enumerate(bad_bodies):
            sig = _sign_body(raw_body, connection.secret)
            response = _post_webhook(
                api_client,
                _webhook_url("github", connection.id),
                raw_body,
                signature=sig,
                delivery_id=f"gh-deliv-bad-json-{idx}",
            )
            assert response.status_code == status.HTTP_400_BAD_REQUEST
            assert response.json() == {"error": "body must be application/json"}

    assert VcsDelivery.objects.filter(connection=connection).count() == 0
    connection.refresh_from_db()
    assert connection.last_delivery_at is None
    mock_delay.assert_not_called()
