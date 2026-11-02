# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from dataclasses import dataclass
from typing import Any
import uuid

from django.db.models import Q

from plane.db.models import Issue
from plane.integrations.vcs.events import PullRequestEvent
from plane.integrations.vcs.settings import compile_pattern


@dataclass(frozen=True)
class MatchedId:
    identifier: str
    sequence_id: int
    source: str


MAX_IDS = 50

SOURCE_FIELD_MAP = {
    "branch": "source_branch",
    "title": "title",
    "body": "body",
}


def extract_ids(event: PullRequestEvent, matchers: list[dict[str, Any]]) -> list[MatchedId]:
    if not matchers:
        return []

    results: list[MatchedId] = []
    seen: set[tuple[str, int]] = set()

    for matcher in matchers:
        if not matcher.get("enabled", True):
            continue

        source = matcher.get("source")
        field_name = SOURCE_FIELD_MAP.get(source)
        if not field_name:
            continue

        text = getattr(event, field_name, "") or ""
        pattern_str = matcher.get("pattern")
        if not pattern_str:
            continue

        compiled = compile_pattern(pattern_str, ignore_case=matcher.get("ignore_case", False))

        for match in compiled.finditer(text):
            ident = match.group(1).upper()
            seq = int(match.group(2))
            key = (ident, seq)
            if key not in seen:
                seen.add(key)
                results.append(MatchedId(identifier=ident, sequence_id=seq, source=source))
                if len(results) >= MAX_IDS:
                    return results

    return results


def resolve_work_items(workspace_id: uuid.UUID | str, ids: list[MatchedId]) -> list[tuple[Issue, MatchedId]]:
    if not ids:
        return []

    q_filter = Q()
    for m in ids:
        q_filter |= Q(project__identifier=m.identifier, sequence_id=m.sequence_id)

    issues = (
        Issue.issue_objects.filter(workspace_id=workspace_id)
        .filter(project__archived_at__isnull=True)
        .filter(q_filter)
        .select_related("project", "state")
    )

    lookup: dict[tuple[str, int], Issue] = {(issue.project.identifier, issue.sequence_id): issue for issue in issues}

    resolved: list[tuple[Issue, MatchedId]] = []
    for m in ids:
        issue = lookup.get((m.identifier, m.sequence_id))
        if issue is not None:
            resolved.append((issue, m))

    return resolved
