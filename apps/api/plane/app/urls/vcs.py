# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.integrations.vcs.webhook import VcsWebhookEndpoint

urlpatterns = [
    path(
        "integrations/vcs/<str:provider>/<uuid:connection_id>/",
        VcsWebhookEndpoint.as_view(),
        name="vcs-webhook",
    ),
]
