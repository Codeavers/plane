# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
import json
from unittest.mock import patch

from django.utils import timezone
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import Issue, IssueSequence, ProjectMember, User, WorkspaceMember
from plane.db.models.vcs import (
    VcsConnection,
    VcsDelivery,
    VcsProjectSettings,
    VcsPullRequest,
    VcsPullRequestLink,
)
from plane.integrations.vcs.bot import create_bot_user
from plane.integrations.vcs.providers.base import VcsProvider
from plane.integrations.vcs.providers.registry import registry
from plane.integrations.vcs.serializers import (
    VcsConnectionSerializer,
    VcsProjectSettingsSerializer,
)
from plane.integrations.vcs.settings import DEFAULT_SETTINGS
from plane.tests.factories import ProjectFactory, UserFactory, WorkspaceFactory

ADMIN, MEMBER, GUEST = 20, 15, 5


def _auth_client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _make_issue(project, author, *, name: str = "Work item") -> Issue:
    # Pass created_by_id explicitly because BaseModel.save clears created_by when crum has no request user.
    issue = Issue(name=name, workspace=project.workspace, project=project)
    issue.save(created_by_id=author.id)
    return issue


@pytest.mark.contract
@pytest.mark.django_db
def test_admin_creates_connection_and_gets_secret_once(session_client, workspace):
    base_url = f"/api/workspaces/{workspace.slug}/vcs-connections/"
    create_resp = session_client.post(
        base_url,
        {
            "provider": "github",
            "name": "Codeavers GitHub",
            "repo_filter": ["Codeavers/*"],
        },
        format="json",
    )
    assert create_resp.status_code == status.HTTP_201_CREATED

    created = create_resp.json()
    conn_id = created["id"]
    assert created["provider"] == "github"
    assert created["name"] == "Codeavers GitHub"
    assert created["repo_filter"] == ["Codeavers/*"]
    assert created["secret"]
    assert created["webhook_url"] == f"http://testserver/api/integrations/vcs/github/{conn_id}/"
    assert created["settings"]["version"] == 1

    conn = VcsConnection.objects.get(id=conn_id)
    assert created["secret"] == conn.secret
    assert "secret" not in VcsConnectionSerializer(conn).data

    detail_url = f"{base_url}{conn_id}/"
    retrieve_resp = session_client.get(detail_url)
    assert retrieve_resp.status_code == status.HTTP_200_OK
    assert "secret" not in retrieve_resp.json()

    list_resp = session_client.get(base_url)
    assert list_resp.status_code == status.HTTP_200_OK
    items = list_resp.json()
    assert len(items) == 1
    assert "secret" not in items[0]

    patch_resp = session_client.patch(
        detail_url,
        {"name": "Renamed GitHub", "secret": "attacker-supplied-secret"},
        format="json",
    )
    assert patch_resp.status_code == status.HTTP_200_OK
    assert patch_resp.json()["name"] == "Renamed GitHub"
    assert "secret" not in patch_resp.json()
    conn.refresh_from_db()
    assert conn.secret == created["secret"]

    delete_resp = session_client.delete(detail_url)
    assert delete_resp.status_code == status.HTTP_204_NO_CONTENT
    assert not VcsConnection.objects.filter(id=conn_id).exists()


@pytest.mark.contract
@pytest.mark.django_db
def test_member_cannot_manage_connections(workspace, create_bot_user):
    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Admin Only Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )

    member_user = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=member_user, role=MEMBER)
    member_client = _auth_client(member_user)

    base_url = f"/api/workspaces/{workspace.slug}/vcs-connections/"
    detail_url = f"{base_url}{conn.id}/"

    assert member_client.get(base_url).status_code == status.HTTP_403_FORBIDDEN
    assert (
        member_client.post(base_url, {"provider": "github", "name": "Nope"}, format="json").status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert member_client.get(detail_url).status_code == status.HTTP_403_FORBIDDEN
    assert member_client.patch(detail_url, {"name": "Nope"}, format="json").status_code == status.HTTP_403_FORBIDDEN
    assert member_client.delete(detail_url).status_code == status.HTTP_403_FORBIDDEN
    assert member_client.post(f"{detail_url}rotate-secret/").status_code == status.HTTP_403_FORBIDDEN
    assert member_client.get(f"{detail_url}deliveries/").status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.contract
@pytest.mark.django_db
def test_invalid_settings_rejected_with_path(session_client, workspace):
    base_url = f"/api/workspaces/{workspace.slug}/vcs-connections/"
    bad_settings = copy.deepcopy(DEFAULT_SETTINGS)
    bad_settings["rules"][0]["on"] = ["not_a_valid_action"]

    resp = session_client.post(
        base_url,
        {"provider": "github", "name": "Invalid Rules", "settings": bad_settings},
        format="json",
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "rules[0]" in json.dumps(resp.json())

    resp_bad_provider = session_client.post(
        base_url,
        {"provider": "unregistered_vcs", "name": "Bad Provider"},
        format="json",
    )
    assert resp_bad_provider.status_code == status.HTTP_400_BAD_REQUEST

    resp_bad_filter = session_client.post(
        base_url,
        {"provider": "github", "name": "Bad Filter", "repo_filter": [""]},
        format="json",
    )
    assert resp_bad_filter.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.contract
@pytest.mark.django_db
def test_rotate_secret_changes_secret(session_client, workspace, create_bot_user):
    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Rotatable Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    old_secret = conn.secret

    rotate_url = f"/api/workspaces/{workspace.slug}/vcs-connections/{conn.id}/rotate-secret/"
    resp = session_client.post(rotate_url)
    assert resp.status_code == status.HTTP_200_OK

    new_secret = resp.json()["secret"]
    assert new_secret
    assert new_secret != old_secret

    conn.refresh_from_db()
    assert conn.secret == new_secret

    detail_resp = session_client.get(f"/api/workspaces/{workspace.slug}/vcs-connections/{conn.id}/")
    assert detail_resp.status_code == status.HTTP_200_OK
    assert "secret" not in detail_resp.json()


@pytest.mark.contract
@pytest.mark.django_db
def test_deliveries_list_scoped_to_workspace(session_client, workspace, create_user, create_bot_user):
    own_conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Own Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    d1 = VcsDelivery.objects.create(
        connection=own_conn,
        delivery_id="deliv-own-1",
        event="pull_request",
        action="opened",
        status=VcsDelivery.STATUS_PROCESSED,
    )
    d2 = VcsDelivery.objects.create(
        connection=own_conn,
        delivery_id="deliv-own-2",
        event="ping",
        action="",
        status=VcsDelivery.STATUS_IGNORED,
    )

    other_workspace = WorkspaceFactory(owner=create_user)
    WorkspaceMember.objects.create(workspace=other_workspace, member=create_user, role=ADMIN)
    other_conn = VcsConnection.objects.create(
        workspace=other_workspace,
        provider="github",
        name="Foreign Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    VcsDelivery.objects.create(
        connection=other_conn,
        delivery_id="deliv-foreign-1",
        event="pull_request",
        action="opened",
        status=VcsDelivery.STATUS_PROCESSED,
    )

    foreign_resp = session_client.get(f"/api/workspaces/{workspace.slug}/vcs-connections/{other_conn.id}/deliveries/")
    assert foreign_resp.status_code == status.HTTP_404_NOT_FOUND

    own_resp = session_client.get(f"/api/workspaces/{workspace.slug}/vcs-connections/{own_conn.id}/deliveries/")
    assert own_resp.status_code == status.HTTP_200_OK
    body = own_resp.json()
    assert "results" in body
    assert {item["id"] for item in body["results"]} == {str(d1.id), str(d2.id)}


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_redeliver_requeues(session_client, workspace, create_user, create_bot_user):
    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Redeliver Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    delivery = VcsDelivery.objects.create(
        connection=conn,
        delivery_id="deliv-failed-001",
        event="pull_request",
        action="opened",
        status=VcsDelivery.STATUS_FAILED,
        summary={"work_items": {"stale": {}}},
        error="previous database error",
        processed_at=timezone.now(),
    )

    other_workspace = WorkspaceFactory(owner=create_user)
    WorkspaceMember.objects.create(workspace=other_workspace, member=create_user, role=ADMIN)
    other_conn = VcsConnection.objects.create(
        workspace=other_workspace,
        provider="github",
        name="Other Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    other_delivery = VcsDelivery.objects.create(
        connection=other_conn,
        delivery_id="deliv-other-001",
        event="pull_request",
        action="opened",
        status=VcsDelivery.STATUS_FAILED,
    )

    url = f"/api/workspaces/{workspace.slug}/vcs-connections/{conn.id}/deliveries/{delivery.id}/redeliver/"
    cross_url = f"/api/workspaces/{workspace.slug}/vcs-connections/{conn.id}/deliveries/{other_delivery.id}/redeliver/"

    with patch("plane.integrations.vcs.tasks.process_vcs_delivery.delay") as mock_delay:
        resp = session_client.post(url)
        cross_resp = session_client.post(cross_url)

    assert resp.status_code == status.HTTP_202_ACCEPTED
    assert cross_resp.status_code == status.HTTP_404_NOT_FOUND

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_RECEIVED
    assert delivery.summary == {}
    assert delivery.error == ""
    mock_delay.assert_called_once_with(str(delivery.id))


@pytest.mark.contract
@pytest.mark.django_db
def test_test_match_returns_resolved_work_items(session_client, workspace, create_user, create_bot_user):
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    IssueSequence.objects.create(project=project, workspace=workspace, sequence=39)
    issue_40 = _make_issue(project, create_user, name="Add GitHub webhook sync")
    issue_41 = _make_issue(project, create_user, name="Add VCS configuration API")
    assert issue_40.sequence_id == 40
    assert issue_41.sequence_id == 41

    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Matcher Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )

    resp = session_client.post(
        f"/api/workspaces/{workspace.slug}/vcs-connections/{conn.id}/test-match/",
        {
            "branch": "feat/codea-40-webhook",
            "title": "feat(vcs): CODEA-40 and UTF-8 support",
            "body": "Fixes CODEA-41",
        },
        format="json",
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == [
        {
            "identifier": "CODEA-40",
            "work_item_id": str(issue_40.id),
            "name": "Add GitHub webhook sync",
            "matched_by": "branch",
        },
        {
            "identifier": "CODEA-41",
            "work_item_id": str(issue_41.id),
            "name": "Add VCS configuration API",
            "matched_by": "body",
        },
    ]


@pytest.mark.contract
@pytest.mark.django_db
def test_project_admin_sets_override(workspace, create_user, create_bot_user):
    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Workspace Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )

    project_admin = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=project_admin, role=MEMBER)
    ProjectMember.objects.create(project=project, workspace=workspace, member=project_admin, role=ADMIN)
    proj_admin_client = _auth_client(project_admin)

    url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/vcs-settings/"
    put_resp = proj_admin_client.put(
        url,
        {
            "connection": str(conn.id),
            "enabled": True,
            "overrides": {"base_branches": ["main", "release/*"]},
        },
        format="json",
    )
    assert put_resp.status_code == status.HTTP_200_OK
    put_data = put_resp.json()
    assert put_data["enabled"] is True
    assert put_data["overrides"] == {"base_branches": ["main", "release/*"]}

    ps = VcsProjectSettings.objects.get(connection=conn, project=project)
    assert ps.enabled is True
    assert ps.overrides == {"base_branches": ["main", "release/*"]}

    get_resp = proj_admin_client.get(url)
    assert get_resp.status_code == status.HTTP_200_OK
    get_data = get_resp.json()
    assert isinstance(get_data, dict)
    assert get_data["enabled"] is True
    assert get_data["overrides"] == {"base_branches": ["main", "release/*"]}

    bad_put_resp = proj_admin_client.put(
        url,
        {
            "connection": str(conn.id),
            "enabled": True,
            "overrides": {"rules": [{"on": ["invalid_action"]}]},
        },
        format="json",
    )
    assert bad_put_resp.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.contract
@pytest.mark.django_db
def test_project_member_cannot_set_override(workspace, create_user, create_bot_user):
    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Workspace Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )

    project_member = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=project_member, role=MEMBER)
    ProjectMember.objects.create(project=project, workspace=workspace, member=project_member, role=MEMBER)
    proj_member_client = _auth_client(project_member)

    url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/vcs-settings/"
    put_resp = proj_member_client.put(
        url,
        {"connection": str(conn.id), "enabled": False, "overrides": {}},
        format="json",
    )
    assert put_resp.status_code == status.HTTP_403_FORBIDDEN
    assert proj_member_client.get(url).status_code == status.HTTP_403_FORBIDDEN
    assert VcsProjectSettings.objects.filter(project=project).count() == 0


@pytest.mark.contract
@pytest.mark.django_db
def test_work_item_pull_requests_visible_to_viewer(workspace, create_user, create_bot_user):
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    issue = _make_issue(project, create_user, name="Visible work item")

    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Workspace Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    linked_pr = VcsPullRequest.objects.create(
        connection=conn,
        repo_full_name="Codeavers/plane",
        number=42,
        url="https://github.com/Codeavers/plane/pull/42",
        title="feat(vcs): CODEA-1 sync",
        source_branch="feat/CODEA-1-sync",
        base_branch="develop",
        author_login="octocat",
        is_draft=False,
        state=VcsPullRequest.STATE_OPEN,
        provider_updated_at=timezone.now(),
    )
    VcsPullRequestLink.objects.create(
        pull_request=linked_pr,
        issue=issue,
        project=project,
        workspace=workspace,
        matched_by=VcsPullRequestLink.MATCHED_BY_BRANCH,
    )

    # Unlinked PR in same workspace and PR from another workspace must not appear.
    VcsPullRequest.objects.create(
        connection=conn,
        repo_full_name="Codeavers/plane",
        number=99,
        url="https://github.com/Codeavers/plane/pull/99",
        title="Unrelated PR",
        source_branch="feat/unrelated",
        base_branch="develop",
        author_login="octocat",
        is_draft=False,
        state=VcsPullRequest.STATE_OPEN,
        provider_updated_at=timezone.now(),
    )
    other_ws = WorkspaceFactory()
    other_conn = VcsConnection.objects.create(
        workspace=other_ws,
        provider="github",
        name="Other WS Connection",
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    foreign_pr = VcsPullRequest.objects.create(
        connection=other_conn,
        repo_full_name="OtherOrg/repo",
        number=7,
        url="https://github.com/OtherOrg/repo/pull/7",
        title="Cross-workspace PR",
        source_branch="feat/cross",
        base_branch="main",
        author_login="mallory",
        is_draft=False,
        state=VcsPullRequest.STATE_OPEN,
        provider_updated_at=timezone.now(),
    )
    VcsPullRequestLink.objects.create(
        pull_request=foreign_pr,
        issue=issue,
        project=project,
        workspace=workspace,
        matched_by=VcsPullRequestLink.MATCHED_BY_BRANCH,
    )

    viewer = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=viewer, role=MEMBER)
    ProjectMember.objects.create(project=project, workspace=workspace, member=viewer, role=MEMBER)
    viewer_client = _auth_client(viewer)

    url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/{issue.id}/pull-requests/"
    resp = viewer_client.get(url)
    assert resp.status_code == status.HTTP_200_OK

    prs = resp.json()
    assert len(prs) == 1
    assert prs[0]["number"] == 42
    assert prs[0]["repo_full_name"] == "Codeavers/plane"
    assert prs[0]["url"] == "https://github.com/Codeavers/plane/pull/42"
    assert prs[0]["state"] == VcsPullRequest.STATE_OPEN
    assert prs[0]["source_branch"] == "feat/CODEA-1-sync"
    assert prs[0]["base_branch"] == "develop"


@pytest.mark.contract
@pytest.mark.django_db
def test_restricted_guest_cannot_see_others_work_item_prs(workspace, create_user, create_bot_user):
    project = ProjectFactory(
        workspace=workspace,
        name="Scoped Project",
        identifier="SCOP",
        created_by=create_user,
        updated_by=create_user,
    )
    assert project.guest_view_all_features is False

    guest = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=guest, role=GUEST)
    ProjectMember.objects.create(project=project, workspace=workspace, member=guest, role=GUEST)
    guest_client = _auth_client(guest)

    foreign_issue = _make_issue(project, create_user, name="Someone else's issue")
    own_issue = _make_issue(project, guest, name="Guest's own issue")

    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Scoped Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    pr = VcsPullRequest.objects.create(
        connection=conn,
        repo_full_name="Codeavers/plane",
        number=50,
        url="https://github.com/Codeavers/plane/pull/50",
        title="feat: PR 50",
        source_branch="feat/pr-50",
        base_branch="develop",
        author_login="octocat",
        is_draft=False,
        state=VcsPullRequest.STATE_OPEN,
        provider_updated_at=timezone.now(),
    )
    VcsPullRequestLink.objects.create(
        pull_request=pr,
        issue=foreign_issue,
        project=project,
        workspace=workspace,
        matched_by=VcsPullRequestLink.MATCHED_BY_BRANCH,
    )
    VcsPullRequestLink.objects.create(
        pull_request=pr,
        issue=own_issue,
        project=project,
        workspace=workspace,
        matched_by=VcsPullRequestLink.MATCHED_BY_BRANCH,
    )

    foreign_url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/{foreign_issue.id}/pull-requests/"
    own_url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/{own_issue.id}/pull-requests/"

    assert guest_client.get(foreign_url).status_code == status.HTTP_403_FORBIDDEN

    own_resp = guest_client.get(own_url)
    assert own_resp.status_code == status.HTTP_200_OK
    assert len(own_resp.json()) == 1

    project.guest_view_all_features = True
    project.save(update_fields=["guest_view_all_features"])
    unrestricted_resp = guest_client.get(foreign_url)
    assert unrestricted_resp.status_code == status.HTTP_200_OK
    assert len(unrestricted_resp.json()) == 1


@pytest.mark.contract
@pytest.mark.django_db
def test_providers_lists_github_with_schema(workspace):
    member = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=member, role=MEMBER)
    member_client = _auth_client(member)

    url = f"/api/workspaces/{workspace.slug}/vcs-providers/"
    resp = member_client.get(url)
    assert resp.status_code == status.HTTP_200_OK

    providers = resp.json()
    assert isinstance(providers, list)
    github = next((p for p in providers if p.get("key") == "github"), None)
    assert github is not None

    defaults = github.get("default_settings") or github.get("defaults")
    assert defaults == DEFAULT_SETTINGS

    schema = github.get("settings_schema") or github.get("schema")
    assert isinstance(schema, dict)
    assert len(schema) > 0

    outsider_client = _auth_client(UserFactory())
    assert outsider_client.get(url).status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.contract
@pytest.mark.django_db
def test_bot_user_created_with_connection(session_client, workspace):
    direct_bot = create_bot_user(workspace)
    assert direct_bot.is_bot is True
    assert direct_bot.bot_type == "VCS"
    assert direct_bot.is_active is True
    assert direct_bot.email.startswith("vcs-")
    assert direct_bot.email.endswith("@plane-bots.local")
    assert not direct_bot.has_usable_password()
    assert WorkspaceMember.objects.filter(
        workspace=workspace,
        member=direct_bot,
        role=15,
        is_active=True,
    ).exists()

    resp = session_client.post(
        f"/api/workspaces/{workspace.slug}/vcs-connections/",
        {"provider": "github", "name": "Connection With Bot"},
        format="json",
    )
    assert resp.status_code == status.HTTP_201_CREATED

    conn = VcsConnection.objects.select_related("bot_user").get(id=resp.json()["id"])
    assert conn.bot_user is not None
    assert conn.bot_user.is_bot is True
    assert conn.bot_user.bot_type == "VCS"
    assert conn.bot_user.is_active is True
    assert conn.bot_user.email.startswith("vcs-")
    assert conn.bot_user.email.endswith("@plane-bots.local")
    assert not conn.bot_user.has_usable_password()
    assert WorkspaceMember.objects.filter(
        workspace=workspace,
        member=conn.bot_user,
        role=15,
        is_active=True,
    ).exists()


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_redeliver_inactive_connection_returns_400(session_client, workspace, create_bot_user):
    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Inactive Redeliver Connection",
        is_active=False,
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    delivery = VcsDelivery.objects.create(
        connection=conn,
        delivery_id="deliv-inactive-redeliver-001",
        event="pull_request",
        action="opened",
        status=VcsDelivery.STATUS_FAILED,
        error="previous error",
    )

    url = f"/api/workspaces/{workspace.slug}/vcs-connections/{conn.id}/deliveries/{delivery.id}/redeliver/"
    with patch("plane.integrations.vcs.tasks.process_vcs_delivery.delay") as mock_delay:
        resp = session_client.post(url)

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json() == {"error": "connection is inactive"}

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_FAILED
    mock_delay.assert_not_called()


@pytest.mark.contract
@pytest.mark.django_db
def test_delete_connection_deactivates_bot_and_membership(session_client, workspace):
    base_url = f"/api/workspaces/{workspace.slug}/vcs-connections/"
    create_resp = session_client.post(
        base_url,
        {"provider": "github", "name": "Deletable Connection"},
        format="json",
    )
    assert create_resp.status_code == status.HTTP_201_CREATED
    conn_id = create_resp.json()["id"]

    conn = VcsConnection.objects.select_related("bot_user").get(id=conn_id)
    bot_user = conn.bot_user
    assert bot_user is not None
    assert bot_user.is_active is True
    assert WorkspaceMember.objects.get(workspace=workspace, member=bot_user).is_active is True

    del_resp = session_client.delete(f"{base_url}{conn_id}/")
    assert del_resp.status_code == status.HTTP_204_NO_CONTENT
    assert not VcsConnection.objects.filter(id=conn_id).exists()
    assert VcsConnection.all_objects.get(id=conn_id).is_active is False

    bot_user.refresh_from_db()
    assert bot_user.is_active is False

    membership = WorkspaceMember.objects.get(workspace=workspace, member=bot_user)
    assert membership.is_active is False


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_create_connection_atomic_rolls_back_bot_user(session_client, workspace):
    base_url = f"/api/workspaces/{workspace.slug}/vcs-connections/"
    with patch.object(VcsConnectionSerializer, "save", side_effect=RuntimeError("db failure")):
        resp = session_client.post(
            base_url,
            {"provider": "github", "name": "Rollback Connection"},
            format="json",
        )

    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert VcsConnection.objects.filter(workspace=workspace).count() == 0
    assert User.objects.filter(is_bot=True, bot_type="VCS").count() == 0
    assert WorkspaceMember.objects.filter(workspace=workspace, member__is_bot=True).count() == 0


@pytest.mark.contract
@pytest.mark.django_db
def test_providers_endpoint_lists_all_registered_providers(workspace):
    class _DummyProvider(VcsProvider):
        key = "gitlab_dummy"

        def verify(self, headers, raw_body, secret):
            return False

        def delivery_id(self, headers):
            return None

        def event_name(self, headers):
            return ""

        def parse(self, headers, payload):
            return None

        def trim_payload(self, headers, payload):
            return {}

        def settings_schema(self):
            return {"type": "object", "title": "GitLab Dummy Schema"}

    member = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=member, role=MEMBER)
    member_client = _auth_client(member)

    with patch.dict(registry._providers, {"gitlab_dummy": _DummyProvider()}):
        resp = member_client.get(f"/api/workspaces/{workspace.slug}/vcs-providers/")

    assert resp.status_code == status.HTTP_200_OK
    by_key = {item["key"]: item for item in resp.json()}
    assert "github" in by_key
    assert "gitlab_dummy" in by_key

    github_provider = registry.get("github")
    github_schema = by_key["github"].get("settings_schema") or by_key["github"].get("schema")
    github_defaults = by_key["github"].get("default_settings") or by_key["github"].get("defaults")
    assert github_schema == github_provider.settings_schema()
    assert github_defaults == DEFAULT_SETTINGS

    dummy_schema = by_key["gitlab_dummy"].get("settings_schema") or by_key["gitlab_dummy"].get("schema")
    dummy_defaults = by_key["gitlab_dummy"].get("default_settings") or by_key["gitlab_dummy"].get("defaults")
    assert dummy_schema == {"type": "object", "title": "GitLab Dummy Schema"}
    assert dummy_defaults == DEFAULT_SETTINGS


@pytest.mark.contract
@pytest.mark.django_db
def test_project_settings_rejects_bad_types(workspace, create_user, create_bot_user):
    conn = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Typed Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    project_admin = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=project_admin, role=MEMBER)
    ProjectMember.objects.create(project=project, workspace=workspace, member=project_admin, role=ADMIN)
    proj_admin_client = _auth_client(project_admin)

    assert not VcsProjectSettingsSerializer(
        data={"connection": str(conn.id), "enabled": True, "overrides": []}
    ).is_valid()
    assert not VcsProjectSettingsSerializer(
        data={"connection": str(conn.id), "enabled": "true", "overrides": {}}
    ).is_valid()

    url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/vcs-settings/"
    invalid_payloads = [
        {"connection": str(conn.id), "enabled": "not-a-bool", "overrides": {}},
        {"connection": str(conn.id), "enabled": "true", "overrides": {}},
        {"connection": str(conn.id), "enabled": 123, "overrides": {}},
        {"connection": str(conn.id), "enabled": ["bad"], "overrides": {}},
        {"connection": str(conn.id), "enabled": True, "overrides": []},
        {"connection": str(conn.id), "enabled": True, "overrides": "not-an-object"},
        {"connection": str(conn.id), "enabled": True, "overrides": 123},
    ]

    for bad_payload in invalid_payloads:
        resp = proj_admin_client.put(url, bad_payload, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    assert VcsProjectSettings.objects.filter(project=project).count() == 0


@pytest.mark.contract
@pytest.mark.django_db
def test_project_settings_get_connection_query_rule(workspace, create_user, create_bot_user):
    conn_1 = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Primary Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    other_ws = WorkspaceFactory()
    VcsConnection.objects.create(
        workspace=other_ws,
        provider="github",
        name="Other Workspace Connection",
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    project_admin = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, member=project_admin, role=MEMBER)
    ProjectMember.objects.create(project=project, workspace=workspace, member=project_admin, role=ADMIN)
    proj_admin_client = _auth_client(project_admin)

    url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/vcs-settings/"
    put_resp = proj_admin_client.put(
        url,
        {"connection": str(conn_1.id), "enabled": False, "overrides": {"base_branches": ["main"]}},
        format="json",
    )
    assert put_resp.status_code == status.HTTP_200_OK

    single_get = proj_admin_client.get(url)
    assert single_get.status_code == status.HTTP_200_OK
    assert single_get.json()["enabled"] is False
    assert single_get.json()["overrides"] == {"base_branches": ["main"]}

    conn_2 = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Secondary Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )

    ambiguous_get = proj_admin_client.get(url)
    assert ambiguous_get.status_code == status.HTTP_400_BAD_REQUEST
    assert ambiguous_get.json() == {"error": "connection query parameter is required"}

    explicit_get_1 = proj_admin_client.get(f"{url}?connection={conn_1.id}")
    assert explicit_get_1.status_code == status.HTTP_200_OK
    assert explicit_get_1.json()["enabled"] is False
    assert explicit_get_1.json()["overrides"] == {"base_branches": ["main"]}

    explicit_get_2 = proj_admin_client.get(f"{url}?connection={conn_2.id}")
    assert explicit_get_2.status_code == status.HTTP_200_OK
    assert explicit_get_2.json()["enabled"] is True
    assert explicit_get_2.json()["overrides"] == {}


@pytest.mark.contract
@pytest.mark.django_db
def test_cross_workspace_connection_returns_404(session_client, workspace, create_user, create_bot_user):
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    ProjectMember.objects.get_or_create(
        project=project,
        workspace=workspace,
        member=create_user,
        defaults={"role": ADMIN},
    )

    other_workspace = WorkspaceFactory(owner=create_user)
    WorkspaceMember.objects.create(workspace=other_workspace, member=create_user, role=ADMIN)
    other_conn = VcsConnection.objects.create(
        workspace=other_workspace,
        provider="github",
        name="Foreign Connection",
        bot_user=create_bot_user,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    original_secret = other_conn.secret

    rotate_resp = session_client.post(
        f"/api/workspaces/{workspace.slug}/vcs-connections/{other_conn.id}/rotate-secret/"
    )
    assert rotate_resp.status_code == status.HTTP_404_NOT_FOUND
    other_conn.refresh_from_db()
    assert other_conn.secret == original_secret

    match_resp = session_client.post(
        f"/api/workspaces/{workspace.slug}/vcs-connections/{other_conn.id}/test-match/",
        {"branch": "feat/CODEA-1", "title": "", "body": ""},
        format="json",
    )
    assert match_resp.status_code == status.HTTP_404_NOT_FOUND

    put_resp = session_client.put(
        f"/api/workspaces/{workspace.slug}/projects/{project.id}/vcs-settings/",
        {"connection": str(other_conn.id), "enabled": True, "overrides": {}},
        format="json",
    )
    assert put_resp.status_code == status.HTTP_404_NOT_FOUND
    assert VcsProjectSettings.objects.filter(project=project).count() == 0
