# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import datetime, timezone

import pytest

from plane.integrations.vcs.events import PullRequestEvent
from plane.integrations.vcs.matchers import MatchedId, extract_ids
from plane.integrations.vcs.settings import DEFAULT_SETTINGS


def _make_event(
    *,
    source_branch: str = "feat/no-id",
    title: str = "chore: update docs",
    body: str = "",
) -> PullRequestEvent:
    return PullRequestEvent(
        provider="github",
        repo_full_name="Codeavers/plane",
        number=42,
        url="https://github.com/Codeavers/plane/pull/42",
        title=title,
        body=body,
        source_branch=source_branch,
        base_branch="develop",
        action="opened",
        is_draft=False,
        author_login="octocat",
        updated_at=datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc),
        merged_at=None,
    )


@pytest.mark.unit
def test_extracts_from_branch_title_body():
    event = _make_event(
        source_branch="feat/CODEA-10-auth",
        title="feat: CODEA-20 and PROJ-30",
        body="Fixes CODEA-40\nCloses PROJ-50",
    )

    matched = extract_ids(event, DEFAULT_SETTINGS["matchers"])

    assert matched == [
        MatchedId(identifier="CODEA", sequence_id=10, source="branch"),
        MatchedId(identifier="CODEA", sequence_id=20, source="title"),
        MatchedId(identifier="PROJ", sequence_id=30, source="title"),
        MatchedId(identifier="CODEA", sequence_id=40, source="body"),
        MatchedId(identifier="PROJ", sequence_id=50, source="body"),
    ]
    assert is_dataclass(matched[0])
    with pytest.raises(FrozenInstanceError):
        matched[0].source = "title"  # type: ignore[misc]


@pytest.mark.unit
def test_branch_matcher_is_case_insensitive():
    event = _make_event(
        source_branch="feat/codea-40-x",
        title="chore: lowercase codea-99 in title is ignored by default",
        body="",
    )

    matched = extract_ids(event, DEFAULT_SETTINGS["matchers"])

    assert matched == [MatchedId(identifier="CODEA", sequence_id=40, source="branch")]


@pytest.mark.unit
def test_disabled_matcher_skipped():
    matchers = copy.deepcopy(DEFAULT_SETTINGS["matchers"])
    for matcher in matchers:
        if matcher["source"] == "branch":
            matcher["enabled"] = False

    event = _make_event(
        source_branch="feat/CODEA-10-auth",
        title="feat: CODEA-20",
        body="Fixes CODEA-30",
    )

    matched = extract_ids(event, matchers)

    assert matched == [
        MatchedId(identifier="CODEA", sequence_id=20, source="title"),
        MatchedId(identifier="CODEA", sequence_id=30, source="body"),
    ]


@pytest.mark.unit
def test_duplicates_keep_first_source():
    event = _make_event(
        source_branch="feat/codea-40-sync",
        title="feat: CODEA-40 and CODEA-41",
        body="Fixes CODEA-40 and resolves CODEA-41",
    )

    matched = extract_ids(event, DEFAULT_SETTINGS["matchers"])

    assert matched == [
        MatchedId(identifier="CODEA", sequence_id=40, source="branch"),
        MatchedId(identifier="CODEA", sequence_id=41, source="title"),
    ]


@pytest.mark.unit
def test_body_requires_keyword_by_default():
    without_keyword = _make_event(body="see CODEA-1")
    assert extract_ids(without_keyword, DEFAULT_SETTINGS["matchers"]) == []

    with_fixes = _make_event(body="Fixes CODEA-1")
    assert extract_ids(with_fixes, DEFAULT_SETTINGS["matchers"]) == [
        MatchedId(identifier="CODEA", sequence_id=1, source="body")
    ]

    with_other_keywords = _make_event(body="closes CODEA-2, RESOLVES CODEA-3, refs CODEA-4")
    assert extract_ids(with_other_keywords, DEFAULT_SETTINGS["matchers"]) == [
        MatchedId(identifier="CODEA", sequence_id=2, source="body"),
        MatchedId(identifier="CODEA", sequence_id=3, source="body"),
        MatchedId(identifier="CODEA", sequence_id=4, source="body"),
    ]


@pytest.mark.unit
def test_extract_ids_caps_at_max_ids():
    body = "\n".join(f"Fixes CODEA-{i}" for i in range(1, 61))
    matched = extract_ids(_make_event(body=body), DEFAULT_SETTINGS["matchers"])
    assert len(matched) == 50
    assert matched == [MatchedId(identifier="CODEA", sequence_id=i, source="body") for i in range(1, 51)]

    ordered = extract_ids(
        _make_event(
            source_branch="feat/CODEA-100-branch",
            title="feat: CODEA-101",
            body=body,
        ),
        DEFAULT_SETTINGS["matchers"],
    )
    assert len(ordered) == 50
    assert ordered[0] == MatchedId(identifier="CODEA", sequence_id=100, source="branch")
    assert ordered[1] == MatchedId(identifier="CODEA", sequence_id=101, source="title")
    assert ordered[2:] == [MatchedId(identifier="CODEA", sequence_id=i, source="body") for i in range(1, 49)]

    from plane.integrations.vcs.matchers import MAX_IDS

    assert MAX_IDS == 50
