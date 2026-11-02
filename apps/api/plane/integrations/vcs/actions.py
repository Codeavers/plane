# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from abc import ABC, abstractmethod
from dataclasses import dataclass
import html
import json
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from plane.api.serializers import IssueSerializer
from plane.bgtasks.issue_activities_task import issue_activity
from plane.db.models import Issue, IssueComment, State
from plane.db.models.vcs import VcsConnection
from plane.integrations.vcs.events import PullRequestEvent


@dataclass
class ActionResult:
    type: str
    status: str
    detail: str = ""


class Action(ABC):
    @abstractmethod
    def execute(
        self,
        issue: Issue,
        event: PullRequestEvent,
        *,
        actor: Any,
        connection: VcsConnection,
        templates: dict[str, str],
    ) -> ActionResult:
        pass


class SetState(Action):
    def __init__(self, state: str, fallback_group: str = "") -> None:
        self.state = state
        self.fallback_group = fallback_group

    def execute(
        self,
        issue: Issue,
        event: PullRequestEvent,
        *,
        actor: Any,
        connection: VcsConnection,
        templates: dict[str, str],
    ) -> ActionResult:
        target = State.objects.filter(project_id=issue.project_id, name__iexact=self.state).order_by("sequence").first()
        if not target and self.fallback_group:
            target = (
                State.objects.filter(project_id=issue.project_id, group=self.fallback_group)
                .order_by("sequence")
                .first()
            )

        if not target:
            return ActionResult(
                type="set_state",
                status="skipped",
                detail=(
                    f"No state matching '{self.state}' or fallback group "
                    f"'{self.fallback_group}' in project {issue.project_id}"
                ),
            )

        if issue.state_id == target.id:
            return ActionResult(
                type="set_state",
                status="noop",
                detail=f"Issue already in state '{target.name}'",
            )

        current_instance = json.dumps(IssueSerializer(issue).data, cls=DjangoJSONEncoder)
        issue.state = target
        issue.updated_by = actor
        issue.save(update_fields=["state", "updated_by", "updated_at"])

        issue_activity.delay(
            type="issue.activity.updated",
            requested_data=json.dumps({"state_id": str(target.id)}),
            actor_id=str(actor.id),
            issue_id=str(issue.id),
            project_id=str(issue.project_id),
            current_instance=current_instance,
            epoch=int(timezone.now().timestamp()),
            notification=True,
            origin=None,
        )

        return ActionResult(
            type="set_state",
            status="applied",
            detail=f"State updated to '{target.name}'",
        )


class Comment(Action):
    def __init__(self, template: str) -> None:
        self.template = template

    def execute(
        self,
        issue: Issue,
        event: PullRequestEvent,
        *,
        actor: Any,
        connection: VcsConnection,
        templates: dict[str, str],
    ) -> ActionResult:
        template_str = templates.get(self.template)
        if not template_str:
            return ActionResult(
                type="comment",
                status="skipped",
                detail=f"Template '{self.template}' not found",
            )

        external_id = f"{connection.id}:{event.repo_full_name}#{event.number}:{event.action}"
        existing = IssueComment.objects.filter(
            issue=issue,
            external_id=external_id,
            external_source="vcs",
        ).first()
        if existing:
            return ActionResult(
                type="comment",
                status="noop",
                detail=f"Comment already exists for {external_id}",
            )

        values = {
            "url": html.escape(str(event.url)),
            "repo": html.escape(str(event.repo_full_name)),
            "number": html.escape(str(event.number)),
            "title": html.escape(str(event.title)),
            "base_branch": html.escape(str(event.base_branch)),
            "source_branch": html.escape(str(event.source_branch)),
            "author": html.escape(str(event.author_login)),
        }
        rendered_html = template_str.format(**values)

        comment = IssueComment.objects.create(
            workspace=issue.workspace,
            project=issue.project,
            issue=issue,
            actor=actor,
            created_by=actor,
            updated_by=actor,
            comment_html=rendered_html,
            external_source="vcs",
            external_id=external_id,
        )

        requested_data = json.dumps(
            {
                "id": str(comment.id),
                "comment_html": comment.comment_html,
                "access": comment.access,
                "external_source": comment.external_source,
                "external_id": comment.external_id,
            },
            cls=DjangoJSONEncoder,
        )

        issue_activity.delay(
            type="comment.activity.created",
            requested_data=requested_data,
            actor_id=str(actor.id),
            issue_id=str(issue.id),
            project_id=str(issue.project_id),
            current_instance=None,
            epoch=int(timezone.now().timestamp()),
        )

        return ActionResult(
            type="comment",
            status="applied",
            detail=f"Comment created for {external_id}",
        )


def build_action(spec: dict[str, Any]) -> Action:
    action_type = spec.get("type")
    if action_type == "set_state":
        return SetState(
            state=spec["state"],
            fallback_group=spec.get("fallback_group", ""),
        )
    elif action_type == "comment":
        return Comment(template=spec["template"])
    raise ValueError(f"Unknown action type: {action_type}")
