# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
from dataclasses import dataclass
import fnmatch
from typing import Any

from plane.integrations.vcs.events import PullRequestEvent


@dataclass(frozen=True)
class RuleContext:
    event: PullRequestEvent
    current_state_group: str
    all_linked_merged: bool
    base_branches: list[str]


def _match_rule(rule: dict[str, Any], ctx: RuleContext, effective_action: str) -> bool:
    if effective_action not in rule.get("on", []):
        return False

    when = rule.get("when", {})

    if "draft" in when:
        if when["draft"] != ctx.event.is_draft:
            return False

    if "from_groups" in when:
        if ctx.current_state_group not in when["from_groups"]:
            return False

    if "all_linked_merged" in when:
        if when["all_linked_merged"] != ctx.all_linked_merged:
            return False

    if "base_branch" in when:
        expected = when["base_branch"]
        if expected == "base_branches":
            patterns = ctx.base_branches
        elif isinstance(expected, list):
            patterns = expected
        else:
            patterns = [expected]

        if not any(fnmatch.fnmatch(ctx.event.base_branch, pat) for pat in patterns):
            return False

    return True


def select_actions(
    settings: dict[str, Any],
    ctx: RuleContext,
    action: str | None = None,
) -> list[dict[str, Any]]:
    effective_action = action if action is not None else ctx.event.action
    selected: list[dict[str, Any]] = []

    for rule in settings.get("rules", []):
        if _match_rule(rule, ctx, effective_action):
            for act in rule.get("actions", []):
                selected.append(copy.deepcopy(act))

    return selected
