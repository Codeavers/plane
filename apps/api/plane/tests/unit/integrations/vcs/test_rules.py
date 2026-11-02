# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import datetime, timezone

import pytest

from plane.integrations.vcs.events import PullRequestEvent
from plane.integrations.vcs.rules import RuleContext, select_actions
from plane.integrations.vcs.settings import DEFAULT_SETTINGS


def _make_event(
    *,
    action: str = "opened",
    is_draft: bool = False,
    base_branch: str = "develop",
) -> PullRequestEvent:
    merged_at = datetime(2026, 10, 29, 12, 30, 0, tzinfo=timezone.utc) if action == "merged" else None
    return PullRequestEvent(
        provider="github",
        repo_full_name="Codeavers/plane",
        number=42,
        url="https://github.com/Codeavers/plane/pull/42",
        title="feat(vcs): CODEA-40 add GitHub webhook sync",
        body="Fixes CODEA-40",
        source_branch="feat/CODEA-40-github-sync",
        base_branch=base_branch,
        action=action,
        is_draft=is_draft,
        author_login="octocat",
        updated_at=datetime(2026, 10, 29, 12, 30, 0, tzinfo=timezone.utc),
        merged_at=merged_at,
    )


@pytest.mark.unit
def test_opened_selects_in_review():
    event = _make_event(action="opened", is_draft=False, base_branch="develop")
    ctx = RuleContext(
        event=event,
        current_state_group="unstarted",
        all_linked_merged=False,
        base_branches=DEFAULT_SETTINGS["base_branches"],
    )

    assert is_dataclass(ctx)
    with pytest.raises(FrozenInstanceError):
        ctx.current_state_group = "completed"  # type: ignore[misc]

    actions = select_actions(DEFAULT_SETTINGS, ctx)

    assert actions == [
        {"type": "set_state", "state": "In Review", "fallback_group": "started"},
        {"type": "comment", "template": "pr_opened"},
    ]


@pytest.mark.unit
def test_draft_opened_selects_nothing():
    event = _make_event(action="opened", is_draft=True, base_branch="develop")
    ctx = RuleContext(
        event=event,
        current_state_group="unstarted",
        all_linked_merged=False,
        base_branches=DEFAULT_SETTINGS["base_branches"],
    )

    assert select_actions(DEFAULT_SETTINGS, ctx) == []


@pytest.mark.unit
def test_merged_into_non_base_branch_selects_nothing():
    non_base_event = _make_event(action="merged", base_branch="feat/stacked-base")
    non_base_ctx = RuleContext(
        event=non_base_event,
        current_state_group="started",
        all_linked_merged=True,
        base_branches=DEFAULT_SETTINGS["base_branches"],
    )
    assert select_actions(DEFAULT_SETTINGS, non_base_ctx) == []

    glob_settings = copy.deepcopy(DEFAULT_SETTINGS)
    glob_settings["base_branches"] = ["release/*"]
    glob_event = _make_event(action="merged", base_branch="release/2026.10")
    glob_ctx = RuleContext(
        event=glob_event,
        current_state_group="started",
        all_linked_merged=True,
        base_branches=glob_settings["base_branches"],
    )
    assert select_actions(glob_settings, glob_ctx) == [
        {"type": "set_state", "state": "Done", "fallback_group": "completed"},
        {"type": "comment", "template": "pr_merged"},
    ]


@pytest.mark.unit
def test_merged_requires_all_linked_merged():
    event = _make_event(action="merged", base_branch="main")

    partial_ctx = RuleContext(
        event=event,
        current_state_group="started",
        all_linked_merged=False,
        base_branches=DEFAULT_SETTINGS["base_branches"],
    )
    assert select_actions(DEFAULT_SETTINGS, partial_ctx) == []

    all_merged_ctx = RuleContext(
        event=event,
        current_state_group="started",
        all_linked_merged=True,
        base_branches=DEFAULT_SETTINGS["base_branches"],
    )
    assert select_actions(DEFAULT_SETTINGS, all_merged_ctx) == [
        {"type": "set_state", "state": "Done", "fallback_group": "completed"},
        {"type": "comment", "template": "pr_merged"},
    ]


@pytest.mark.unit
def test_from_groups_blocks_cancelled():
    reopened_event = _make_event(action="reopened", is_draft=False)
    cancelled_ctx = RuleContext(
        event=reopened_event,
        current_state_group="cancelled",
        all_linked_merged=False,
        base_branches=DEFAULT_SETTINGS["base_branches"],
    )
    assert select_actions(DEFAULT_SETTINGS, cancelled_ctx) == []

    merged_event = _make_event(action="merged", base_branch="main")
    merged_cancelled_ctx = RuleContext(
        event=merged_event,
        current_state_group="cancelled",
        all_linked_merged=True,
        base_branches=DEFAULT_SETTINGS["base_branches"],
    )
    assert select_actions(DEFAULT_SETTINGS, merged_cancelled_ctx) == []


@pytest.mark.unit
def test_action_override_uses_given_action():
    edited_event = _make_event(action="edited", is_draft=False, base_branch="develop")
    ctx = RuleContext(
        event=edited_event,
        current_state_group="backlog",
        all_linked_merged=False,
        base_branches=DEFAULT_SETTINGS["base_branches"],
    )

    assert select_actions(DEFAULT_SETTINGS, ctx) == []

    overridden = select_actions(DEFAULT_SETTINGS, ctx, action="opened")
    assert overridden == [
        {"type": "set_state", "state": "In Review", "fallback_group": "started"},
        {"type": "comment", "template": "pr_opened"},
    ]

    multi_rule_settings = copy.deepcopy(DEFAULT_SETTINGS)
    multi_rule_settings["rules"].append(
        {
            "on": ["opened"],
            "when": {"draft": False},
            "actions": [{"type": "comment", "template": "pr_closed"}],
        }
    )
    assert select_actions(multi_rule_settings, ctx, action="opened") == [
        {"type": "set_state", "state": "In Review", "fallback_group": "started"},
        {"type": "comment", "template": "pr_opened"},
        {"type": "comment", "template": "pr_closed"},
    ]
