# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from plane.db.models import (
    Issue,
    Project,
    User,
    VcsConnection,
    VcsDelivery,
    VcsProjectSettings,
    VcsPullRequest,
    VcsPullRequestLink,
    Workspace,
)


@pytest.fixture
def test_user(db):
    uid = uuid4().hex[:8]
    return User.objects.create(email=f"user-{uid}@plane.so", username=f"user_{uid}")


@pytest.fixture
def workspace(db, test_user):
    uid = uuid4().hex[:8]
    return Workspace.objects.create(name=f"Workspace {uid}", owner=test_user, slug=f"ws-{uid}")


@pytest.fixture
def project(db, workspace, test_user):
    uid = uuid4().hex[:4].upper()
    return Project.objects.create(
        name=f"Project {uid}",
        identifier=f"P{uid}",
        workspace=workspace,
        created_by=test_user,
    )


@pytest.fixture
def issue(db, project, workspace, test_user):
    return Issue.objects.create(
        name="Test Issue",
        workspace=workspace,
        project=project,
        created_by=test_user,
    )


@pytest.fixture
def connection(db, workspace):
    return VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="GitHub Connection",
    )


@pytest.mark.contract
@pytest.mark.django_db
def test_pull_request_unique_per_connection_repo_number(connection):
    VcsPullRequest.objects.create(
        connection=connection,
        repo_full_name="org/repo",
        number=101,
        url="https://github.com/org/repo/pull/101",
        title="Feature 101",
        source_branch="feat/101",
        base_branch="main",
        author_login="octocat",
        is_draft=False,
        state=VcsPullRequest.STATE_OPEN,
        provider_updated_at=timezone.now(),
    )
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            VcsPullRequest.objects.create(
                connection=connection,
                repo_full_name="org/repo",
                number=101,
                url="https://github.com/org/repo/pull/101-dup",
                title="Feature 101 Duplicate",
                source_branch="feat/101-dup",
                base_branch="main",
                author_login="octocat",
                is_draft=False,
                state=VcsPullRequest.STATE_OPEN,
                provider_updated_at=timezone.now(),
            )


@pytest.mark.contract
@pytest.mark.django_db
def test_link_unique_per_pull_request_and_issue(connection, issue):
    pr = VcsPullRequest.objects.create(
        connection=connection,
        repo_full_name="org/repo",
        number=102,
        url="https://github.com/org/repo/pull/102",
        title="Feature 102",
        source_branch="feat/102",
        base_branch="main",
        author_login="octocat",
        is_draft=False,
        state=VcsPullRequest.STATE_OPEN,
        provider_updated_at=timezone.now(),
    )
    VcsPullRequestLink.objects.create(
        pull_request=pr,
        issue=issue,
        matched_by=VcsPullRequestLink.MATCHED_BY_BRANCH,
    )
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            VcsPullRequestLink.objects.create(
                pull_request=pr,
                issue=issue,
                matched_by=VcsPullRequestLink.MATCHED_BY_TITLE,
            )


@pytest.mark.contract
@pytest.mark.django_db
def test_delivery_unique_per_connection_and_delivery_id(connection):
    VcsDelivery.objects.create(
        connection=connection,
        delivery_id="delivery-uuid-001",
        event="pull_request",
        action="opened",
        status=VcsDelivery.STATUS_RECEIVED,
    )
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            VcsDelivery.objects.create(
                connection=connection,
                delivery_id="delivery-uuid-001",
                event="pull_request",
                action="opened",
                status=VcsDelivery.STATUS_RECEIVED,
            )


@pytest.mark.contract
@pytest.mark.django_db
def test_soft_deleted_link_allows_relink(connection, issue):
    pr = VcsPullRequest.objects.create(
        connection=connection,
        repo_full_name="org/repo",
        number=103,
        url="https://github.com/org/repo/pull/103",
        title="Feature 103",
        source_branch="feat/103",
        base_branch="main",
        author_login="octocat",
        is_draft=False,
        state=VcsPullRequest.STATE_OPEN,
        provider_updated_at=timezone.now(),
    )
    link = VcsPullRequestLink.objects.create(
        pull_request=pr,
        issue=issue,
        matched_by=VcsPullRequestLink.MATCHED_BY_BRANCH,
    )
    link.delete()
    assert link.deleted_at is not None

    new_link = VcsPullRequestLink.objects.create(
        pull_request=pr,
        issue=issue,
        matched_by=VcsPullRequestLink.MATCHED_BY_TITLE,
    )
    assert new_link.id != link.id
    assert new_link.deleted_at is None


@pytest.mark.contract
@pytest.mark.django_db
def test_connection_secret_is_generated(workspace):
    c1 = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Connection 1",
    )
    c2 = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Connection 2",
    )
    assert c1.secret
    assert c2.secret
    assert c1.secret != c2.secret


@pytest.mark.contract
@pytest.mark.django_db
def test_project_settings_unique_per_connection_and_project(connection, project):
    VcsProjectSettings.objects.create(
        connection=connection,
        project=project,
    )
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            VcsProjectSettings.objects.create(
                connection=connection,
                project=project,
            )
