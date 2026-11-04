# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

from plane.db.models import User, Workspace, WorkspaceMember


def create_bot_user(workspace: Workspace) -> User:
    hex_id = uuid4().hex
    bot = User(
        email=f"vcs-{hex_id}@plane-bots.local",
        username=hex_id,
        first_name="VCS",
        last_name="Bot",
        is_bot=True,
        bot_type="VCS",
        is_active=True,
    )
    bot.set_unusable_password()
    bot.save()

    WorkspaceMember.objects.create(
        workspace=workspace,
        member=bot,
        role=15,
        is_active=True,
    )

    return bot
