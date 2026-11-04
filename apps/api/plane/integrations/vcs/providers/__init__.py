# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from plane.integrations.vcs.providers.github import GitHubProvider
from plane.integrations.vcs.providers.registry import registry

registry.register(GitHubProvider())

__all__ = ["registry"]
