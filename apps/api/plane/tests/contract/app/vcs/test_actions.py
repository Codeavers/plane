# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import datetime, timezone
import json
from unittest.mock import patch

from django.db import transaction
import pytest

from plane.db.models import Issue, IssueComment, State
from plane.db.models.vcs import VcsConnection
from plane.integrations.vcs.actions import (
    Action,
    ActionResult,
    Comment,
    SetState,
    build_action,
)
from plane.integrations.vcs.events import PullRequestEvent
from plane.integrations.vcs.settings import DEFAULT_SETTINGS
from plane.tests.factories import ProjectFactory


def _make_event(
    *,
    title: str = "feat(vcs): CODEA-1 add GitHub webhook sync",
    action: str = "opened",
    base_branch: str = "develop",
    source_branch: str = "feat/CODEA-1-github-sync",
    author_login: str = "octocat",
    number: int = 42,
) -> PullRequestEvent:
    return PullRequestEvent(
        provider="github",
        repo_full_name="Codeavers/plane",
        number=number,
        url=f"https://github.com/Codeavers/plane/pull/{number}",
        title=title,
        body="Fixes CODEA-1",
        source_branch=source_branch,
        base_branch=base_branch,
        action=action,
        is_draft=False,
        author_login=author_login,
        updated_at=datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc),
        merged_at=datetime(2026, 10, 29, 12, 30, 0, tzinfo=timezone.utc) if action == "merged" else None,
    )


@pytest.fixture
def mock_issue_activity():
    with patch("plane.integrations.vcs.actions.issue_activity") as mocked:
        yield mocked


@pytest.fixture
def project(workspace, create_user):
    return ProjectFactory(
        workspace=workspace,
        name="Codeaver",
        identifier="CODEA",
        created_by=create_user,
        updated_by=create_user,
    )


@pytest.fixture
def project_states(project, create_user):
    # State.save() assigns strictly increasing sequences in creation order,
    # ensuring "In Progress" precedes "In Review" within the "started" group.
    specs = [
        ("Backlog", "backlog", "#60646C", True, 15000),
        ("Todo", "unstarted", "#60646C", False, 25000),
        ("In Progress", "started", "#F59E0B", False, 35000),
        ("In Review", "started", "#3B82F6", False, 40000),
        ("Done", "completed", "#46A758", False, 45000),
        ("Cancelled", "cancelled", "#9AA4BC", False, 55000),
    ]
    states = {}
    for name, group, color, is_default, seq in specs:
        states[name] = State.objects.create(
            project=project,
            name=name,
            group=group,
            color=color,
            default=is_default,
            sequence=seq,
            created_by=create_user,
        )
    return states


@pytest.fixture
def connection(workspace, create_bot_user):
    return VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="GitHub Organization",
        bot_user=create_bot_user,
        settings=DEFAULT_SETTINGS,
    )


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_set_state_by_name(project, project_states, connection, create_user, create_bot_user, mock_issue_activity):
    issue = Issue.objects.create(
        name="Implement VCS actions",
        workspace=project.workspace,
        project=project,
        state=project_states["Todo"],
        created_by=create_user,
    )
    action = build_action({"type": "set_state", "state": "in review", "fallback_group": "started"})
    assert isinstance(action, Action)
    assert isinstance(action, SetState)

    result = action.execute(
        issue,
        _make_event(),
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )

    assert isinstance(result, ActionResult)
    assert result.type == "set_state"
    assert result.status == "applied"

    issue.refresh_from_db()
    assert issue.state_id == project_states["In Review"].id

    mock_issue_activity.delay.assert_called_once()
    call_kwargs = mock_issue_activity.delay.call_args.kwargs
    assert call_kwargs["type"] == "issue.activity.updated"
    assert json.loads(call_kwargs["requested_data"]) == {"state_id": str(project_states["In Review"].id)}
    assert call_kwargs["actor_id"] == str(create_bot_user.id)
    assert call_kwargs["issue_id"] == str(issue.id)
    assert call_kwargs["project_id"] == str(project.id)
    current_instance = json.loads(call_kwargs["current_instance"])
    assert str(current_instance.get("state_id") or current_instance.get("state")) == str(project_states["Todo"].id)
    assert isinstance(call_kwargs["epoch"], (int, float))
    assert call_kwargs["notification"] is True
    assert call_kwargs.get("origin") is None


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_set_state_falls_back_to_group(
    project, project_states, connection, create_user, create_bot_user, mock_issue_activity
):
    assert project_states["In Progress"].sequence < project_states["In Review"].sequence
    issue = Issue.objects.create(
        name="Fallback state test",
        workspace=project.workspace,
        project=project,
        state=project_states["Todo"],
        created_by=create_user,
    )

    action = SetState(state="QA Verification", fallback_group="started")
    result = action.execute(
        issue,
        _make_event(),
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )

    assert result.type == "set_state"
    assert result.status == "applied"

    issue.refresh_from_db()
    assert issue.state_id == project_states["In Progress"].id
    mock_issue_activity.delay.assert_called_once()


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_set_state_skipped_when_no_state(workspace, connection, create_user, create_bot_user, mock_issue_activity):
    minimal_project = ProjectFactory(
        workspace=workspace,
        name="Minimal Project",
        identifier="MIN",
        created_by=create_user,
        updated_by=create_user,
    )
    todo_state = State.objects.create(
        project=minimal_project,
        name="Todo",
        group="unstarted",
        color="#60646C",
        default=True,
        created_by=create_user,
    )
    issue = Issue.objects.create(
        name="No completed state exists",
        workspace=workspace,
        project=minimal_project,
        state=todo_state,
        created_by=create_user,
    )

    action = SetState(state="Done", fallback_group="completed")
    result = action.execute(
        issue,
        _make_event(action="merged"),
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )

    assert result.type == "set_state"
    assert result.status == "skipped"

    issue.refresh_from_db()
    assert issue.state_id == todo_state.id
    mock_issue_activity.delay.assert_not_called()


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_set_state_noop_when_already_there(
    project, project_states, connection, create_user, create_bot_user, mock_issue_activity
):
    issue = Issue.objects.create(
        name="Already in review",
        workspace=project.workspace,
        project=project,
        state=project_states["In Review"],
        created_by=create_user,
    )

    action = SetState(state="In Review", fallback_group="started")
    result = action.execute(
        issue,
        _make_event(),
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )

    assert result.type == "set_state"
    assert result.status == "noop"

    issue.refresh_from_db()
    assert issue.state_id == project_states["In Review"].id
    mock_issue_activity.delay.assert_not_called()


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_comment_escapes_title(project, project_states, connection, create_user, create_bot_user, mock_issue_activity):
    issue = Issue.objects.create(
        name="Escape HTML in PR title",
        workspace=project.workspace,
        project=project,
        state=project_states["Todo"],
        created_by=create_user,
    )
    event = _make_event(title="<script>alert(1)</script>", action="opened")

    action = build_action({"type": "comment", "template": "pr_opened"})
    assert isinstance(action, Action)
    assert isinstance(action, Comment)

    result = action.execute(
        issue,
        event,
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )

    assert result.type == "comment"
    assert result.status == "applied"

    comment = IssueComment.objects.get(issue=issue)
    assert "<script>" not in comment.comment_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in comment.comment_html
    assert '<a href="https://github.com/Codeavers/plane/pull/42">Codeavers/plane#42</a>' in comment.comment_html
    assert comment.actor_id == create_bot_user.id
    assert comment.external_source == "vcs"
    assert comment.external_id == f"{connection.id}:Codeavers/plane#42:opened"

    mock_issue_activity.delay.assert_called_once()
    call_kwargs = mock_issue_activity.delay.call_args.kwargs
    assert call_kwargs["type"] == "comment.activity.created"
    assert call_kwargs["actor_id"] == str(create_bot_user.id)
    assert call_kwargs["issue_id"] == str(issue.id)
    assert call_kwargs["project_id"] == str(project.id)
    assert call_kwargs["current_instance"] is None
    assert json.loads(call_kwargs["requested_data"])["comment_html"] == comment.comment_html


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_comment_not_duplicated_for_same_event(
    project, project_states, connection, create_user, create_bot_user, mock_issue_activity
):
    issue = Issue.objects.create(
        name="Deduplicate PR comment",
        workspace=project.workspace,
        project=project,
        state=project_states["Todo"],
        created_by=create_user,
    )
    opened_event = _make_event(action="opened")
    action = Comment(template="pr_opened")

    first = action.execute(
        issue,
        opened_event,
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )
    assert first.type == "comment"
    assert first.status == "applied"
    assert IssueComment.objects.filter(issue=issue).count() == 1
    assert mock_issue_activity.delay.call_count == 1

    second = action.execute(
        issue,
        opened_event,
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )
    assert second.type == "comment"
    assert second.status == "noop"
    assert IssueComment.objects.filter(issue=issue).count() == 1
    assert mock_issue_activity.delay.call_count == 1

    merged_action = Comment(template="pr_merged")
    merged_result = merged_action.execute(
        issue,
        _make_event(action="merged"),
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )
    assert merged_result.status == "applied"
    assert IssueComment.objects.filter(issue=issue).count() == 2


@pytest.mark.contract
@pytest.mark.django_db
def test_set_state_preserves_concurrent_name_change(
    project, project_states, connection, create_user, create_bot_user, mock_issue_activity
):
    issue = Issue.objects.create(
        name="Original work item name",
        workspace=project.workspace,
        project=project,
        state=project_states["Todo"],
        created_by=create_user,
    )
    Issue.objects.filter(pk=issue.pk).update(name="Concurrent work item name")

    action = SetState(state="Done", fallback_group="completed")
    result = action.execute(
        issue,
        _make_event(action="merged"),
        actor=create_bot_user,
        connection=connection,
        templates=DEFAULT_SETTINGS["comment_templates"],
    )

    assert result.type == "set_state"
    assert result.status == "applied"

    issue.refresh_from_db()
    assert issue.name == "Concurrent work item name"
    assert issue.state_id == project_states["Done"].id
    assert issue.completed_at is not None


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_comment_failure_rolls_back_state_and_skips_issue_activity(
    project, project_states, connection, create_user, create_bot_user, mock_issue_activity
):
    issue = Issue.objects.create(
        name="Rollback suppresses on_commit activity",
        workspace=project.workspace,
        project=project,
        state=project_states["Todo"],
        created_by=create_user,
    )
    event = _make_event(action="opened")

    with (
        patch.object(IssueComment.objects, "create", side_effect=RuntimeError("comment write failed")),
        pytest.raises(RuntimeError, match="comment write failed"),
        transaction.atomic(),
    ):
        state_res = SetState(state="In Review", fallback_group="started").execute(
            issue,
            event,
            actor=create_bot_user,
            connection=connection,
            templates=DEFAULT_SETTINGS["comment_templates"],
        )
        assert state_res.status == "applied"
        Comment(template="pr_opened").execute(
            issue,
            event,
            actor=create_bot_user,
            connection=connection,
            templates=DEFAULT_SETTINGS["comment_templates"],
        )

    issue.refresh_from_db()
    assert issue.state_id == project_states["Todo"].id
    mock_issue_activity.delay.assert_not_called()

    with pytest.raises(RuntimeError, match="abort after comment"), transaction.atomic():
        comment_res = Comment(template="pr_opened").execute(
            issue,
            event,
            actor=create_bot_user,
            connection=connection,
            templates=DEFAULT_SETTINGS["comment_templates"],
        )
        assert comment_res.status == "applied"
        raise RuntimeError("abort after comment")

    assert IssueComment.objects.filter(issue=issue).count() == 0
    mock_issue_activity.delay.assert_not_called()
