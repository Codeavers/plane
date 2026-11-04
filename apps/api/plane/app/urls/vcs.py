# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.integrations.vcs.views import (
    VcsConnectionDetailEndpoint,
    VcsConnectionEndpoint,
    VcsDeliveryEndpoint,
    VcsIssuePullRequestsEndpoint,
    VcsProjectSettingsEndpoint,
    VcsProvidersEndpoint,
    VcsRedeliverEndpoint,
    VcsRotateSecretEndpoint,
    VcsTestMatchEndpoint,
)
from plane.integrations.vcs.webhook import VcsWebhookEndpoint

urlpatterns = [
    # Webhook
    path(
        "integrations/vcs/<str:provider>/<uuid:connection_id>/",
        VcsWebhookEndpoint.as_view(),
        name="vcs-webhook",
    ),
    # Providers
    path(
        "workspaces/<str:slug>/vcs-providers/",
        VcsProvidersEndpoint.as_view(),
        name="vcs-providers",
    ),
    # Connections
    path(
        "workspaces/<str:slug>/vcs-connections/",
        VcsConnectionEndpoint.as_view(),
        name="vcs-connections",
    ),
    path(
        "workspaces/<str:slug>/vcs-connections/<uuid:connection_id>/",
        VcsConnectionDetailEndpoint.as_view(),
        name="vcs-connection-detail",
    ),
    path(
        "workspaces/<str:slug>/vcs-connections/<uuid:connection_id>/rotate-secret/",
        VcsRotateSecretEndpoint.as_view(),
        name="vcs-rotate-secret",
    ),
    path(
        "workspaces/<str:slug>/vcs-connections/<uuid:connection_id>/test-match/",
        VcsTestMatchEndpoint.as_view(),
        name="vcs-test-match",
    ),
    # Deliveries
    path(
        "workspaces/<str:slug>/vcs-connections/<uuid:connection_id>/deliveries/",
        VcsDeliveryEndpoint.as_view(),
        name="vcs-deliveries",
    ),
    path(
        "workspaces/<str:slug>/vcs-connections/<uuid:connection_id>/deliveries/<uuid:delivery_id>/redeliver/",
        VcsRedeliverEndpoint.as_view(),
        name="vcs-redeliver",
    ),
    # Project Settings
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/vcs-settings/",
        VcsProjectSettingsEndpoint.as_view(),
        name="vcs-project-settings",
    ),
    # Work item PR list
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/issues/<uuid:issue_id>/pull-requests/",
        VcsIssuePullRequestsEndpoint.as_view(),
        name="vcs-issue-pull-requests",
    ),
]
