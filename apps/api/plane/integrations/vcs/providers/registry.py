# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from plane.integrations.vcs.providers.base import VcsProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, VcsProvider] = {}

    def register(self, provider: VcsProvider) -> None:
        self._providers[provider.key] = provider

    def __contains__(self, key: object) -> bool:
        return key in self._providers

    def get(self, key: str) -> VcsProvider:
        if key not in self._providers:
            raise KeyError(f"VCS provider '{key}' not found")
        return self._providers[key]

    def all(self) -> list[VcsProvider]:
        return list(self._providers.values())


registry = ProviderRegistry()
