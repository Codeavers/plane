# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PingEvent:
    provider: str


@dataclass(frozen=True)
class PullRequestEvent:
    provider: str
    repo_full_name: str
    number: int
    url: str
    title: str
    body: str
    source_branch: str
    base_branch: str
    action: str
    is_draft: bool
    author_login: str
    updated_at: datetime
    merged_at: datetime | None = None
