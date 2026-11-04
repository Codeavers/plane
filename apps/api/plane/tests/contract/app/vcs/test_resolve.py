# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.utils import timezone
import pytest

from plane.db.models import Issue, State
from plane.integrations.vcs.matchers import MatchedId, resolve_work_items
from plane.tests.factories import ProjectFactory, WorkspaceFactory


def _create_issue_in_project(project, *, name: str = "Work item") -> Issue:
    backlog, _ = State.objects.get_or_create(
        project=project,
        name="Backlog",
        defaults={
            "color": "#60646C",
            "group": "backlog",
            "default": True,
            "created_by": project.created_by,
        },
    )
    return Issue.objects.create(
        name=name,
        workspace=project.workspace,
        project=project,
        state=backlog,
        created_by=project.created_by,
    )


@pytest.mark.contract
@pytest.mark.django_db
def test_resolves_by_identifier_and_sequence(workspace, create_user):
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    _create_issue_in_project(project, name="First item")
    second_issue = _create_issue_in_project(project, name="Second item")
    assert second_issue.sequence_id == 2

    archived_project = ProjectFactory(
        workspace=workspace,
        name="Archived Project",
        identifier="ARCH",
        archived_at=timezone.now(),
        created_by=create_user,
        updated_by=create_user,
    )
    archived_issue = _create_issue_in_project(archived_project, name="Archived item")

    matched = MatchedId(identifier="CODEA", sequence_id=2, source="branch")
    archived_matched = MatchedId(identifier="ARCH", sequence_id=archived_issue.sequence_id, source="title")

    resolved = resolve_work_items(workspace.id, [matched, archived_matched])

    assert resolved == [(second_issue, matched)]


@pytest.mark.contract
@pytest.mark.django_db
def test_does_not_resolve_other_workspace(workspace, create_user):
    own_project = ProjectFactory(
        workspace=workspace,
        name="Codeaver Main",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    own_issue = _create_issue_in_project(own_project, name="Own workspace issue")

    other_workspace = WorkspaceFactory()
    other_same_id_project = ProjectFactory(
        workspace=other_workspace,
        name="Codeaver Other",
        identifier="CODEA",
    )
    other_same_id_issue = _create_issue_in_project(other_same_id_project, name="Other workspace CODEA-1")
    assert own_issue.sequence_id == other_same_id_issue.sequence_id == 1

    other_only_project = ProjectFactory(
        workspace=other_workspace,
        name="Other Only",
        identifier="OTHER",
    )
    other_only_issue = _create_issue_in_project(other_only_project, name="Other workspace OTHER-1")

    ids = [
        MatchedId(identifier="CODEA", sequence_id=1, source="branch"),
        MatchedId(identifier="OTHER", sequence_id=other_only_issue.sequence_id, source="title"),
    ]
    resolved = resolve_work_items(workspace.id, ids)

    assert len(resolved) == 1
    resolved_issue, resolved_match = resolved[0]
    assert resolved_issue.id == own_issue.id
    assert resolved_issue.workspace_id == workspace.id
    assert resolved_match == MatchedId(identifier="CODEA", sequence_id=1, source="branch")


@pytest.mark.contract
@pytest.mark.django_db
def test_unresolved_ids_omitted(workspace, create_user):
    project = ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )
    existing_issue = _create_issue_in_project(project, name="Existing issue")

    ids = [
        MatchedId(identifier="UTF", sequence_id=8, source="body"),
        MatchedId(identifier="CODEA", sequence_id=999, source="title"),
        MatchedId(identifier="CODEA", sequence_id=existing_issue.sequence_id, source="branch"),
    ]
    resolved = resolve_work_items(workspace.id, ids)

    assert resolved == [
        (existing_issue, MatchedId(identifier="CODEA", sequence_id=existing_issue.sequence_id, source="branch"))
    ]
    assert resolve_work_items(workspace.id, []) == []
