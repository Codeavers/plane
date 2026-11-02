# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
from datetime import datetime, timedelta, timezone as dt_timezone
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from celery.schedules import crontab
from django.db import InterfaceError, OperationalError
from django.utils import timezone
import pytest

from plane.celery import app as celery_app
from plane.db.models import Issue, IssueComment, IssueSequence, State
from plane.db.models.vcs import (
    VcsConnection,
    VcsDelivery,
    VcsProjectSettings,
    VcsPullRequest,
    VcsPullRequestLink,
)
from plane.integrations.vcs.actions import Comment
from plane.integrations.vcs.providers.registry import registry
from plane.integrations.vcs.service import process_delivery
from plane.integrations.vcs.settings import DEFAULT_SETTINGS
from plane.integrations.vcs.tasks import process_vcs_delivery, purge_vcs_deliveries
from plane.tests.factories import ProjectFactory

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "unit" / "integrations" / "vcs" / "fixtures" / "github"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _seed_sequence_before_40(project) -> None:
    if not IssueSequence.objects.filter(project=project).exists():
        IssueSequence.objects.create(project=project, workspace=project.workspace, sequence=39)


def _make_issue(project, state, author, *, name: str = "Sync GitHub pull requests") -> Issue:
    # Pass created_by_id explicitly because BaseModel.save clears created_by when crum has no request user.
    issue = Issue(name=name, workspace=project.workspace, project=project, state=state)
    issue.save(created_by_id=author.id)
    return issue


def _make_delivery(
    connection: VcsConnection,
    fixture_name: str,
    *,
    event: str = "pull_request",
    delivery_id: str | None = None,
    mutate=None,
) -> VcsDelivery:
    payload = _load_fixture(fixture_name)
    if mutate is not None:
        mutate(payload)
    action = payload.get("action", "") if event == "pull_request" else ""
    return VcsDelivery.objects.create(
        connection=connection,
        delivery_id=delivery_id or f"gh-deliv-{uuid4().hex[:10]}",
        event=event,
        action=action,
        status=VcsDelivery.STATUS_RECEIVED,
        payload=payload,
    )


@pytest.fixture(autouse=True)
def eager_celery(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True


@pytest.fixture(autouse=True)
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
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )


@pytest.mark.contract
@pytest.mark.django_db
def test_opened_moves_to_in_review_and_comments(
    project, project_states, connection, create_user, create_bot_user, mock_issue_activity
):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user)
    assert issue_40.sequence_id == 40

    delivery = _make_delivery(connection, "opened.json")
    process_vcs_delivery.delay(str(delivery.id))

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["In Review"].id

    pr = VcsPullRequest.objects.get(connection=connection, repo_full_name="Codeavers/plane", number=42)
    assert pr.state == VcsPullRequest.STATE_OPEN
    assert pr.is_draft is False
    assert pr.source_branch == "feat/CODEA-40-github-sync"
    assert pr.base_branch == "develop"
    assert pr.author_login == "octocat"

    link = VcsPullRequestLink.objects.get(pull_request=pr, issue=issue_40)
    assert link.matched_by == VcsPullRequestLink.MATCHED_BY_BRANCH
    assert link.project_id == project.id

    comment = IssueComment.objects.get(issue=issue_40, external_source="vcs")
    assert comment.actor_id == create_bot_user.id
    assert comment.external_id == f"{connection.id}:Codeavers/plane#42:opened"
    assert "Codeavers/plane#42" in comment.comment_html

    assert mock_issue_activity.delay.call_count == 2

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_PROCESSED
    assert delivery.processed_at is not None
    assert delivery.error == ""

    item_summary = delivery.summary["work_items"][str(issue_40.id)]
    assert item_summary["identifier"] == "CODEA-40"
    results = item_summary["results"]
    assert any(r["type"] == "set_state" and r["status"] == "applied" and "In Review" in r["detail"] for r in results)
    assert any(r["type"] == "comment" and r["status"] == "applied" for r in results)


@pytest.mark.contract
@pytest.mark.django_db
def test_merged_moves_to_done(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user)

    opened_delivery = _make_delivery(connection, "opened.json")
    process_delivery(opened_delivery)

    merged_delivery = _make_delivery(connection, "closed_merged.json")
    process_delivery(merged_delivery)

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["Done"].id

    pr = VcsPullRequest.objects.get(connection=connection, repo_full_name="Codeavers/plane", number=42)
    assert pr.state == VcsPullRequest.STATE_MERGED
    assert pr.merged_at == datetime(2026, 10, 29, 12, 30, 0, tzinfo=dt_timezone.utc)

    merged_comment = IssueComment.objects.get(
        issue=issue_40,
        external_source="vcs",
        external_id=f"{connection.id}:Codeavers/plane#42:merged",
    )
    assert "merged into develop" in merged_comment.comment_html

    merged_delivery.refresh_from_db()
    assert merged_delivery.status == VcsDelivery.STATUS_PROCESSED


@pytest.mark.contract
@pytest.mark.django_db
def test_closed_unmerged_moves_back_to_in_progress(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user)

    process_delivery(_make_delivery(connection, "opened.json"))
    closed_delivery = _make_delivery(connection, "closed_unmerged.json")
    process_delivery(closed_delivery)

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["In Progress"].id

    pr = VcsPullRequest.objects.get(connection=connection, repo_full_name="Codeavers/plane", number=42)
    assert pr.state == VcsPullRequest.STATE_CLOSED
    assert pr.merged_at is None

    closed_comment = IssueComment.objects.get(
        issue=issue_40,
        external_source="vcs",
        external_id=f"{connection.id}:Codeavers/plane#42:closed",
    )
    assert "closed without merge" in closed_comment.comment_html

    closed_delivery.refresh_from_db()
    assert closed_delivery.status == VcsDelivery.STATUS_PROCESSED


@pytest.mark.contract
@pytest.mark.django_db
def test_done_waits_for_all_linked_prs(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user)

    def _as_pr_43(payload: dict) -> None:
        payload["number"] = 43
        payload["pull_request"]["number"] = 43
        payload["pull_request"]["html_url"] = "https://github.com/Codeavers/plane/pull/43"

    process_delivery(_make_delivery(connection, "opened.json"))
    process_delivery(_make_delivery(connection, "opened.json", mutate=_as_pr_43))

    # A soft-deleted link on an unmerged PR must not block completion.
    unlinked_pr = VcsPullRequest.objects.create(
        connection=connection,
        repo_full_name="Codeavers/plane",
        number=99,
        url="https://github.com/Codeavers/plane/pull/99",
        title="Old PR",
        source_branch="feat/old",
        base_branch="develop",
        author_login="octocat",
        is_draft=False,
        state=VcsPullRequest.STATE_OPEN,
        provider_updated_at=timezone.now(),
    )
    deleted_link = VcsPullRequestLink.objects.create(
        pull_request=unlinked_pr,
        issue=issue_40,
        matched_by=VcsPullRequestLink.MATCHED_BY_BRANCH,
    )
    deleted_link.delete()

    process_delivery(_make_delivery(connection, "closed_merged.json"))
    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["In Review"].id

    process_delivery(_make_delivery(connection, "closed_merged.json", mutate=_as_pr_43))
    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["Done"].id


@pytest.mark.contract
@pytest.mark.django_db
def test_merge_into_feature_branch_does_not_finish(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["In Review"], create_user)

    def _into_feature_branch(payload: dict) -> None:
        payload["pull_request"]["base"]["ref"] = "feat/epic-branch"

    delivery = _make_delivery(connection, "closed_merged.json", mutate=_into_feature_branch)
    process_delivery(delivery)

    pr = VcsPullRequest.objects.get(connection=connection, repo_full_name="Codeavers/plane", number=42)
    assert pr.state == VcsPullRequest.STATE_MERGED

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["In Review"].id
    assert not IssueComment.objects.filter(issue=issue_40, external_source="vcs").exists()

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_PROCESSED


@pytest.mark.contract
@pytest.mark.django_db
def test_cancelled_work_item_untouched_on_reopen(project, project_states, connection, create_user, mock_issue_activity):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Cancelled"], create_user)

    delivery = _make_delivery(connection, "reopened.json")
    process_delivery(delivery)

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["Cancelled"].id
    assert IssueComment.objects.filter(issue=issue_40).count() == 0
    mock_issue_activity.delay.assert_not_called()

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_PROCESSED


@pytest.mark.contract
@pytest.mark.django_db
def test_repo_outside_filter_ignored(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user)

    connection.repo_filter = ["Codeavers/other-*", "Acme/*"]
    connection.save()

    delivery = _make_delivery(connection, "opened.json")
    process_delivery(delivery)

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_IGNORED
    assert delivery.processed_at is not None
    assert VcsPullRequest.objects.filter(connection=connection).count() == 0

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["Todo"].id


@pytest.mark.contract
@pytest.mark.django_db
def test_stale_event_does_not_regress_pr_state(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["In Review"], create_user)

    merged_delivery = _make_delivery(connection, "closed_merged.json")
    process_delivery(merged_delivery)

    pr = VcsPullRequest.objects.get(connection=connection, repo_full_name="Codeavers/plane", number=42)
    assert pr.state == VcsPullRequest.STATE_MERGED
    latest_updated_at = pr.provider_updated_at

    # Move issue back to Todo so we can verify stale opened event runs no rules.
    issue_40.state = project_states["Todo"]
    issue_40.save()

    stale_delivery = _make_delivery(connection, "opened.json")
    process_delivery(stale_delivery)

    pr.refresh_from_db()
    assert pr.state == VcsPullRequest.STATE_MERGED
    assert pr.provider_updated_at == latest_updated_at

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["Todo"].id
    assert not IssueComment.objects.filter(
        issue=issue_40,
        external_id=f"{connection.id}:Codeavers/plane#42:opened",
    ).exists()

    stale_delivery.refresh_from_db()
    assert stale_delivery.processed_at is not None
    assert "stale" in json.dumps(stale_delivery.summary)


@pytest.mark.contract
@pytest.mark.django_db
def test_link_added_by_edit_on_open_pr_applies_opened_rules(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user, name="Original work item")
    issue_41 = _make_issue(project, project_states["Todo"], create_user, name="Added work item")
    assert issue_40.sequence_id == 40
    assert issue_41.sequence_id == 41

    process_delivery(_make_delivery(connection, "opened.json"))
    issue_40.refresh_from_db()
    issue_41.refresh_from_db()
    assert issue_40.state_id == project_states["In Review"].id
    assert issue_41.state_id == project_states["Todo"].id

    # Move already-linked CODEA-40 to In Progress to verify edit does not re-run opened rules on existing links.
    issue_40.state = project_states["In Progress"]
    issue_40.save()

    edited_delivery = _make_delivery(connection, "edited.json")
    process_delivery(edited_delivery)

    issue_40.refresh_from_db()
    issue_41.refresh_from_db()
    assert issue_40.state_id == project_states["In Progress"].id
    assert issue_41.state_id == project_states["In Review"].id
    assert IssueComment.objects.filter(issue=issue_41, external_source="vcs").count() == 1


@pytest.mark.contract
@pytest.mark.django_db
def test_unresolved_identifiers_are_ignored(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user)

    def _add_unresolved_tokens(payload: dict) -> None:
        payload["pull_request"]["title"] = "feat(vcs): CODEA-40 support UTF-8 and SHA-256"

    delivery = _make_delivery(connection, "opened.json", mutate=_add_unresolved_tokens)
    process_delivery(delivery)

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["In Review"].id

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_PROCESSED
    assert delivery.error == ""
    assert "UTF-8" in delivery.summary["unresolved"]
    assert "SHA-256" in delivery.summary["unresolved"]
    assert str(issue_40.id) in delivery.summary["work_items"]


@pytest.mark.contract
@pytest.mark.django_db
def test_project_override_changes_target_state(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    qa_state = State.objects.create(
        project=project,
        name="QA Review",
        group="started",
        color="#8B5CF6",
        default=False,
        sequence=42000,
        created_by=create_user,
    )
    issue_40 = _make_issue(project, project_states["Todo"], create_user)

    VcsProjectSettings.objects.create(
        connection=connection,
        project=project,
        workspace=project.workspace,
        enabled=True,
        overrides={
            "rules": [
                {
                    "on": ["opened"],
                    "when": {"draft": False, "from_groups": ["backlog", "unstarted", "started"]},
                    "actions": [{"type": "set_state", "state": "QA Review", "fallback_group": "started"}],
                }
            ]
        },
    )

    delivery = _make_delivery(connection, "opened.json")
    process_delivery(delivery)

    issue_40.refresh_from_db()
    assert issue_40.state_id == qa_state.id
    assert IssueComment.objects.filter(issue=issue_40).count() == 0

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_PROCESSED


@pytest.mark.contract
@pytest.mark.django_db
def test_disabled_project_settings_skips_project(workspace, project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user)

    VcsProjectSettings.objects.create(
        connection=connection,
        project=project,
        workspace=workspace,
        enabled=False,
        overrides={},
    )

    delivery = _make_delivery(connection, "opened.json")
    process_delivery(delivery)

    issue_40.refresh_from_db()
    assert issue_40.state_id == project_states["Todo"].id
    assert VcsPullRequestLink.objects.filter(issue=issue_40).count() == 0

    # Also verify only_enabled_projects skips projects without an enabled VcsProjectSettings row.
    other_project = ProjectFactory(
        workspace=workspace,
        name="Other Project",
        identifier="OTHER",
        created_by=create_user,
        updated_by=create_user,
    )
    other_todo = State.objects.create(
        project=other_project,
        name="Todo",
        group="unstarted",
        color="#60646C",
        default=True,
        created_by=create_user,
    )
    other_review = State.objects.create(
        project=other_project,
        name="In Review",
        group="started",
        color="#3B82F6",
        default=False,
        created_by=create_user,
    )
    other_issue = _make_issue(other_project, other_todo, create_user, name="Other item")
    assert other_issue.sequence_id == 1

    connection.only_enabled_projects = True
    connection.save()

    def _ref_other(payload: dict) -> None:
        payload["number"] = 44
        payload["pull_request"]["number"] = 44
        payload["pull_request"]["head"]["ref"] = "feat/OTHER-1-test"
        payload["pull_request"]["title"] = "feat: OTHER-1 test"
        payload["pull_request"]["body"] = ""

    process_delivery(_make_delivery(connection, "opened.json", mutate=_ref_other))
    other_issue.refresh_from_db()
    assert other_issue.state_id == other_todo.id

    VcsProjectSettings.objects.create(
        connection=connection,
        project=other_project,
        workspace=workspace,
        enabled=True,
        overrides={},
    )
    process_delivery(
        _make_delivery(
            connection,
            "opened.json",
            mutate=lambda p: (
                _ref_other(p),
                p["pull_request"].update({"updated_at": "2026-10-29T10:30:00Z"}),
            ),
        )
    )
    other_issue.refresh_from_db()
    assert other_issue.state_id == other_review.id


@pytest.mark.contract
@pytest.mark.django_db
def test_failure_on_one_work_item_does_not_stop_others(project, project_states, connection, create_user):
    _seed_sequence_before_40(project)
    issue_40 = _make_issue(project, project_states["Todo"], create_user, name="Failing item")
    issue_41 = _make_issue(project, project_states["Todo"], create_user, name="Succeeding item")

    def _ref_both_opened(payload: dict) -> None:
        payload["pull_request"]["title"] = "feat(vcs): CODEA-40 and CODEA-41 sync"

    original_execute = Comment.execute

    def _fail_comment_on_40(self, issue, event, **kwargs):
        if issue.id == issue_40.id:
            raise RuntimeError("simulated failure on CODEA-40")
        return original_execute(self, issue, event, **kwargs)

    delivery = _make_delivery(connection, "opened.json", mutate=_ref_both_opened)
    with patch.object(Comment, "execute", autospec=True, side_effect=_fail_comment_on_40):
        process_delivery(delivery)

    # Per-item transaction.atomic() rolls back SetState on issue_40 while issue_41 commits cleanly.
    issue_40.refresh_from_db()
    issue_41.refresh_from_db()
    assert issue_40.state_id == project_states["Todo"].id
    assert issue_41.state_id == project_states["In Review"].id
    assert IssueComment.objects.filter(issue=issue_41, external_source="vcs").count() == 1

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_PROCESSED
    summary_40 = delivery.summary["work_items"][str(issue_40.id)]
    summary_41 = delivery.summary["work_items"][str(issue_41.id)]
    assert "error" in summary_40 or any(r.get("status") == "failed" for r in summary_40.get("results", []))
    assert any(r["type"] == "set_state" and r["status"] == "applied" for r in summary_41["results"])


@pytest.mark.contract
@pytest.mark.django_db
def test_ping_marks_connection_verified(connection):
    assert connection.verified_at is None

    delivery = _make_delivery(connection, "ping.json", event="ping")
    process_delivery(delivery)

    connection.refresh_from_db()
    assert connection.verified_at is not None

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_IGNORED
    assert delivery.processed_at is not None


@pytest.mark.contract
@pytest.mark.django_db
def test_purge_removes_old_deliveries_only(connection, settings):
    old_delivery = _make_delivery(connection, "ping.json", event="ping", delivery_id="old-deliv")
    recent_delivery = _make_delivery(connection, "ping.json", event="ping", delivery_id="recent-deliv")

    now = timezone.now()
    VcsDelivery.all_objects.filter(pk=old_delivery.pk).update(created_at=now - timedelta(days=31))
    VcsDelivery.all_objects.filter(pk=recent_delivery.pk).update(created_at=now - timedelta(days=29))

    purge_vcs_deliveries()

    assert not VcsDelivery.all_objects.filter(pk=old_delivery.pk).exists()
    assert VcsDelivery.all_objects.filter(pk=recent_delivery.pk).exists()

    assert "plane.integrations.vcs.tasks" in settings.CELERY_IMPORTS
    beat_matches = [
        entry
        for entry in celery_app.conf.beat_schedule.values()
        if entry.get("task") == "plane.integrations.vcs.tasks.purge_vcs_deliveries"
    ]
    assert len(beat_matches) == 1
    assert beat_matches[0]["schedule"] == crontab(hour=3, minute=15)


@pytest.mark.contract
@pytest.mark.django_db
def test_missing_bot_user_marks_delivery_failed(workspace):
    conn_without_bot = VcsConnection.objects.create(
        workspace=workspace,
        provider="github",
        name="Botless Connection",
        bot_user=None,
        settings=copy.deepcopy(DEFAULT_SETTINGS),
    )
    delivery = _make_delivery(conn_without_bot, "opened.json")
    process_delivery(delivery)

    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_FAILED
    assert delivery.error == "connection has no bot user"
    assert delivery.processed_at is not None


@pytest.mark.contract
@pytest.mark.django_db
def test_celery_task_retries_transient_error_and_marks_failed(connection):
    assert OperationalError in process_vcs_delivery.autoretry_for
    assert InterfaceError in process_vcs_delivery.autoretry_for
    assert bool(process_vcs_delivery.retry_backoff) is True
    assert process_vcs_delivery.max_retries == 3

    delivery = _make_delivery(connection, "opened.json")
    with patch.object(registry, "get", side_effect=OperationalError("database connection lost")) as mock_get:
        try:
            process_vcs_delivery.apply(args=[str(delivery.id)])
        except OperationalError:
            pass

    assert mock_get.call_count == 4
    delivery.refresh_from_db()
    assert delivery.status == VcsDelivery.STATUS_FAILED
    assert "database connection lost" in delivery.error
