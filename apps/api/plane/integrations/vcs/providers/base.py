# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from plane.integrations.vcs.events import PingEvent, PullRequestEvent


class VcsProvider(ABC):
    key: str

    @abstractmethod
    def verify(self, headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool:
        pass

    @abstractmethod
    def delivery_id(self, headers: Mapping[str, str]) -> str | None:
        pass

    @abstractmethod
    def event_name(self, headers: Mapping[str, str]) -> str:
        pass

    @abstractmethod
    def parse(self, headers: Mapping[str, str], payload: dict[str, Any]) -> PullRequestEvent | PingEvent | None:
        pass

    @abstractmethod
    def trim_payload(self, headers: Mapping[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        pass
