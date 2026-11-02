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

from plane.db.models import Issue, IssueSequence, ProjectMember, WorkspaceMember
from plane.db.models.vcs import (
    VcsConnection,
    VcsDelivery,
    VcsProjectSettings,
    VcsPullRequest,
    VcsPullRequestLink,
)
from plane.integrations.vcs.bot import create_bot_user
from plane.integrations.vcs.serializers import VcsConnectionSerializer
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

    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_202_ACCEPTED)
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
