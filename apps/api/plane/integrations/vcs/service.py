# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import dataclasses
import fnmatch
from typing import Any

from django.db import transaction
from django.utils import timezone

from plane.db.models import Issue
from plane.db.models.vcs import (
    VcsDelivery,
    VcsProjectSettings,
    VcsPullRequest,
    VcsPullRequestLink,
)
from plane.integrations.vcs.actions import Comment, SetState
from plane.integrations.vcs.events import PingEvent, PullRequestEvent
from plane.integrations.vcs.matchers import extract_ids, resolve_work_items
from plane.integrations.vcs.providers.registry import registry
from plane.integrations.vcs.rules import RuleContext, select_actions
from plane.integrations.vcs.settings import DEFAULT_SETTINGS, merge_settings


def process_delivery(delivery: VcsDelivery) -> None:
    delivery.attempts += 1
    connection = delivery.connection

    if connection.bot_user is None:
        delivery.status = VcsDelivery.STATUS_FAILED
        delivery.error = "connection has no bot user"
        delivery.processed_at = timezone.now()
        delivery.save(update_fields=["status", "error", "processed_at", "attempts", "updated_at"])
        return

    provider = registry.get(connection.provider)
    headers = {
        "X-GitHub-Event": delivery.event,
        "X-Event-Key": delivery.event,
        "X-Gitlab-Event": delivery.event,
    }
    parsed = provider.parse(headers, delivery.payload)

    if isinstance(parsed, PingEvent):
        now = timezone.now()
        connection.verified_at = now
        connection.save(update_fields=["verified_at", "updated_at"])
        delivery.status = VcsDelivery.STATUS_IGNORED
        delivery.processed_at = now
        delivery.save(update_fields=["status", "processed_at", "attempts", "updated_at"])
        return

    if not isinstance(parsed, PullRequestEvent):
        delivery.status = VcsDelivery.STATUS_IGNORED
        delivery.processed_at = timezone.now()
        delivery.save(update_fields=["status", "processed_at", "attempts", "updated_at"])
        return

    if connection.repo_filter:
        repo_matched = any(
            fnmatch.fnmatchcase(parsed.repo_full_name, pat) or fnmatch.fnmatch(parsed.repo_full_name, pat)
            for pat in connection.repo_filter
        )
        if not repo_matched:
            delivery.status = VcsDelivery.STATUS_IGNORED
            delivery.processed_at = timezone.now()
            delivery.save(update_fields=["status", "processed_at", "attempts", "updated_at"])
            return

    with transaction.atomic():
        pr, created = VcsPullRequest.objects.select_for_update().get_or_create(
            connection=connection,
            repo_full_name=parsed.repo_full_name,
            number=parsed.number,
            defaults={
                "url": parsed.url,
                "title": parsed.title,
                "source_branch": parsed.source_branch,
                "base_branch": parsed.base_branch,
                "author_login": parsed.author_login,
                "is_draft": parsed.is_draft,
                "state": (
                    VcsPullRequest.STATE_MERGED
                    if parsed.action == "merged"
                    else (
                        VcsPullRequest.STATE_CLOSED
                        if parsed.action == "closed"
                        else VcsPullRequest.STATE_OPEN
                    )
                ),
                "merged_at": parsed.merged_at,
                "provider_updated_at": parsed.updated_at,
            },
        )
        if not created:
            if parsed.updated_at < pr.provider_updated_at:
                delivery.status = VcsDelivery.STATUS_PROCESSED
                delivery.summary = {
                    "stale": True,
                    "detail": "event updated_at is older than latest recorded",
                }
                delivery.processed_at = timezone.now()
                delivery.save(update_fields=["status", "summary", "processed_at", "attempts", "updated_at"])
                return

            pr.url = parsed.url
            pr.title = parsed.title
            pr.source_branch = parsed.source_branch
            pr.base_branch = parsed.base_branch
            pr.author_login = parsed.author_login
            pr.is_draft = parsed.is_draft
            if parsed.action == "merged":
                pr.state = VcsPullRequest.STATE_MERGED
                pr.merged_at = parsed.merged_at
            elif parsed.action == "closed":
                pr.state = VcsPullRequest.STATE_CLOSED
            elif parsed.action in (
                "opened",
                "reopened",
                "ready_for_review",
                "converted_to_draft",
                "synchronize",
                "edited",
            ):
                pr.state = VcsPullRequest.STATE_OPEN
            pr.provider_updated_at = parsed.updated_at
            pr.save(
                update_fields=[
                    "url",
                    "title",
                    "source_branch",
                    "base_branch",
                    "author_login",
                    "is_draft",
                    "state",
                    "merged_at",
                    "provider_updated_at",
                    "updated_at",
                ]
            )

    matchers = connection.settings.get("matchers", DEFAULT_SETTINGS["matchers"])
    matched_ids = extract_ids(parsed, matchers)
    resolved_pairs = resolve_work_items(connection.workspace_id, matched_ids)

    resolved_keys = {(issue.project.identifier, issue.sequence_id) for issue, _ in resolved_pairs}
    unresolved = [
        f"{m.identifier}-{m.sequence_id}"
        for m in matched_ids
        if (m.identifier, m.sequence_id) not in resolved_keys
    ]
    unresolved = list(dict.fromkeys(unresolved))

    work_items_summary: dict[str, Any] = {}

    for issue_item, matched_id in resolved_pairs:
        issue_id_str = str(issue_item.id)
        work_item_ident = f"{issue_item.project.identifier}-{issue_item.sequence_id}"

        proj_settings = VcsProjectSettings.objects.filter(
            connection=connection,
            project=issue_item.project,
        ).first()

        if connection.only_enabled_projects:
            if not proj_settings or not proj_settings.enabled:
                continue
        else:
            if proj_settings and not proj_settings.enabled:
                continue

        try:
            with transaction.atomic():
                locked_issue = Issue.issue_objects.select_for_update().get(pk=issue_item.pk)

                link, link_created = VcsPullRequestLink.objects.get_or_create(
                    pull_request=pr,
                    issue=locked_issue,
                    defaults={
                        "matched_by": matched_id.source,
                        "project": locked_issue.project,
                    },
                )

                if (
                    link_created
                    and pr.state == VcsPullRequest.STATE_OPEN
                    and not pr.is_draft
                    and parsed.action != "opened"
                ):
                    eval_action = "opened"
                else:
                    eval_action = parsed.action

                linked_prs = VcsPullRequestLink.objects.filter(issue=locked_issue).select_related("pull_request")
                all_linked_merged = linked_prs.exists() and all(
                    l.pull_request.state == VcsPullRequest.STATE_MERGED for l in linked_prs
                )

                if proj_settings and proj_settings.overrides:
                    effective_settings = merge_settings(connection.settings, proj_settings.overrides)
                else:
                    effective_settings = connection.settings

                templates = effective_settings.get("comment_templates", DEFAULT_SETTINGS["comment_templates"])
                base_branches = effective_settings.get("base_branches", DEFAULT_SETTINGS["base_branches"])

                rule_ctx = RuleContext(
                    event=parsed,
                    current_state_group=locked_issue.state.group if locked_issue.state else "",
                    all_linked_merged=all_linked_merged,
                    base_branches=base_branches,
                )

                actions_to_run = select_actions(effective_settings, rule_ctx, action=eval_action)
                action_event = (
                    dataclasses.replace(parsed, action=eval_action) if eval_action != parsed.action else parsed
                )

                results = []
                for act_def in actions_to_run:
                    act_type = act_def.get("type")
                    if act_type == "set_state":
                        action_obj = SetState(
                            state=act_def["state"],
                            fallback_group=act_def.get("fallback_group", ""),
                        )
                    elif act_type == "comment":
                        action_obj = Comment(template=act_def["template"])
                    else:
                        continue

                    res = action_obj.execute(
                        locked_issue,
                        action_event,
                        actor=connection.bot_user,
                        connection=connection,
                        templates=templates,
                    )
                    results.append({"type": res.type, "status": res.status, "detail": res.detail})

                work_items_summary[issue_id_str] = {
                    "identifier": work_item_ident,
                    "results": results,
                }
        except Exception as e:
            work_items_summary[issue_id_str] = {
                "identifier": work_item_ident,
                "results": [{"status": "failed", "detail": str(e)}],
                "error": str(e),
            }

    delivery.summary = {
        "work_items": work_items_summary,
        "unresolved": unresolved,
    }
    delivery.status = VcsDelivery.STATUS_PROCESSED
    delivery.processed_at = timezone.now()
    delivery.error = ""
    delivery.save(update_fields=["status", "summary", "error", "processed_at", "attempts", "updated_at"])
