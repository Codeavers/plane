# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from plane.app.permissions import (
    ProjectAdminPermission,
    ProjectEntityPermission,
    ROLE,
    WorkSpaceAdminPermission,
    WorkspaceUserPermission,
    allow_permission,
    issue_hidden_from_guest,
)
from plane.app.views.base import BaseAPIView
from plane.db.models import Workspace
from plane.db.models.api import generate_token
from plane.db.models.vcs import (
    VcsConnection,
    VcsDelivery,
    VcsProjectSettings,
    VcsPullRequestLink,
)
from plane.integrations.vcs.bot import create_bot_user
from plane.integrations.vcs.events import PullRequestEvent
from plane.integrations.vcs.matchers import extract_ids, resolve_work_items
from plane.integrations.vcs.serializers import (
    VcsConnectionSerializer,
    VcsDeliverySerializer,
)
from plane.integrations.vcs.settings import (
    DEFAULT_SETTINGS,
    SettingsError,
    validate_settings as validate_vcs_settings,
)
from plane.integrations.vcs.tasks import process_vcs_delivery


class VcsProvidersEndpoint(BaseAPIView):
    permission_classes = [WorkspaceUserPermission]

    def get(self, request, slug: str) -> Response:
        providers_data = [
            {
                "key": "github",
                "name": "GitHub",
                "default_settings": DEFAULT_SETTINGS,
                "settings_schema": {
                    "type": "object",
                    "properties": {
                        "version": {"type": "integer"},
                        "matchers": {"type": "array"},
                        "base_branches": {"type": "array"},
                        "rules": {"type": "array"},
                        "comment_templates": {"type": "object"},
                    },
                },
            }
        ]
        return Response(providers_data, status=status.HTTP_200_OK)


class VcsConnectionEndpoint(BaseAPIView):
    permission_classes = [WorkSpaceAdminPermission]

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def get(self, request, slug: str) -> Response:
        connections = VcsConnection.objects.filter(workspace__slug=slug)
        serializer = VcsConnectionSerializer(connections, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def post(self, request, slug: str) -> Response:
        workspace = get_object_or_404(Workspace, slug=slug)
        serializer = VcsConnectionSerializer(data=request.data)
        if serializer.is_valid():
            bot_user = create_bot_user(workspace)
            conn = serializer.save(workspace=workspace, bot_user=bot_user)
            data = dict(serializer.data)
            data["secret"] = conn.secret
            data["webhook_url"] = f"{request.build_absolute_uri('/')}api/integrations/vcs/{conn.provider}/{conn.id}/"
            return Response(data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class VcsConnectionDetailEndpoint(BaseAPIView):
    permission_classes = [WorkSpaceAdminPermission]

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def get(self, request, slug: str, connection_id: str) -> Response:
        conn = get_object_or_404(VcsConnection, pk=connection_id, workspace__slug=slug)
        serializer = VcsConnectionSerializer(conn)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def patch(self, request, slug: str, connection_id: str) -> Response:
        conn = get_object_or_404(VcsConnection, pk=connection_id, workspace__slug=slug)
        serializer = VcsConnectionSerializer(conn, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def delete(self, request, slug: str, connection_id: str) -> Response:
        conn = get_object_or_404(VcsConnection, pk=connection_id, workspace__slug=slug)
        conn.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class VcsRotateSecretEndpoint(BaseAPIView):
    permission_classes = [WorkSpaceAdminPermission]

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def post(self, request, slug: str, connection_id: str) -> Response:
        conn = get_object_or_404(VcsConnection, pk=connection_id, workspace__slug=slug)
        conn.secret = generate_token()
        conn.save(update_fields=["secret", "updated_at"])
        return Response({"secret": conn.secret}, status=status.HTTP_200_OK)


class VcsDeliveryEndpoint(BaseAPIView):
    permission_classes = [WorkSpaceAdminPermission]

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def get(self, request, slug: str, connection_id: str) -> Response:
        conn = get_object_or_404(VcsConnection, pk=connection_id, workspace__slug=slug)
        deliveries = VcsDelivery.objects.filter(connection=conn).order_by("-created_at")
        return self.paginate(
            request=request,
            queryset=deliveries,
            on_results=lambda items: VcsDeliverySerializer(items, many=True).data,
        )


class VcsRedeliverEndpoint(BaseAPIView):
    permission_classes = [WorkSpaceAdminPermission]

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def post(self, request, slug: str, connection_id: str, delivery_id: str) -> Response:
        delivery = get_object_or_404(
            VcsDelivery,
            pk=delivery_id,
            connection_id=connection_id,
            connection__workspace__slug=slug,
        )
        delivery.status = VcsDelivery.STATUS_RECEIVED
        delivery.summary = {}
        delivery.error = ""
        delivery.save(update_fields=["status", "summary", "error", "updated_at"])
        process_vcs_delivery.delay(str(delivery.id))
        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)


class VcsTestMatchEndpoint(BaseAPIView):
    permission_classes = [WorkSpaceAdminPermission]

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def post(self, request, slug: str, connection_id: str) -> Response:
        conn = get_object_or_404(VcsConnection, pk=connection_id, workspace__slug=slug)
        branch = request.data.get("branch", "")
        title = request.data.get("title", "")
        body = request.data.get("body", "")

        event = PullRequestEvent(
            provider=conn.provider,
            repo_full_name="",
            number=0,
            url="",
            title=title,
            body=body,
            source_branch=branch,
            base_branch="",
            action="opened",
            is_draft=False,
            author_login="",
            updated_at=timezone.now(),
            merged_at=None,
        )
        matchers = conn.settings.get("matchers", DEFAULT_SETTINGS["matchers"])
        matched_ids = extract_ids(event, matchers)
        resolved_pairs = resolve_work_items(conn.workspace_id, matched_ids)

        results = [
            {
                "identifier": f"{issue.project.identifier}-{issue.sequence_id}",
                "work_item_id": str(issue.id),
                "name": issue.name,
                "matched_by": matched_id.source,
            }
            for issue, matched_id in resolved_pairs
        ]
        return Response(results, status=status.HTTP_200_OK)


class VcsProjectSettingsEndpoint(BaseAPIView):
    permission_classes = [ProjectAdminPermission]

    def get(self, request, slug: str, project_id: str) -> Response:
        connection_id = request.query_params.get("connection")
        if connection_id:
            ps = VcsProjectSettings.objects.filter(
                project_id=project_id,
                connection_id=connection_id,
                workspace__slug=slug,
            ).first()
        else:
            ps = VcsProjectSettings.objects.filter(
                project_id=project_id,
                workspace__slug=slug,
            ).first()

        if not ps:
            return Response({"enabled": True, "overrides": {}}, status=status.HTTP_200_OK)

        return Response(
            {
                "id": str(ps.id),
                "connection": str(ps.connection_id),
                "enabled": ps.enabled,
                "overrides": ps.overrides,
            },
            status=status.HTTP_200_OK,
        )

    def put(self, request, slug: str, project_id: str) -> Response:
        connection_id = request.data.get("connection")
        if not connection_id:
            return Response({"error": "connection is required"}, status=status.HTTP_400_BAD_REQUEST)

        conn = get_object_or_404(VcsConnection, pk=connection_id, workspace__slug=slug)
        overrides = request.data.get("overrides", {})
        if overrides:
            try:
                overrides = validate_vcs_settings(overrides, partial=True)
            except SettingsError as e:
                return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        enabled = request.data.get("enabled", True)
        ps, _ = VcsProjectSettings.objects.update_or_create(
            connection=conn,
            project_id=project_id,
            defaults={
                "workspace": conn.workspace,
                "enabled": enabled,
                "overrides": overrides,
            },
        )
        return Response(
            {
                "id": str(ps.id),
                "connection": str(ps.connection_id),
                "enabled": ps.enabled,
                "overrides": ps.overrides,
            },
            status=status.HTTP_200_OK,
        )


class VcsIssuePullRequestsEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    def get(self, request, slug: str, project_id: str, issue_id: str) -> Response:
        if issue_hidden_from_guest(request, slug, project_id, issue_id):
            return Response(
                {"error": "You don't have the required permissions."},
                status=status.HTTP_403_FORBIDDEN,
            )

        links = VcsPullRequestLink.objects.filter(
            issue_id=issue_id,
            project_id=project_id,
            workspace__slug=slug,
            pull_request__connection__workspace__slug=slug,
        ).select_related("pull_request")

        prs = [
            {
                "id": str(link.pull_request.id),
                "number": link.pull_request.number,
                "repo_full_name": link.pull_request.repo_full_name,
                "url": link.pull_request.url,
                "title": link.pull_request.title,
                "state": link.pull_request.state,
                "source_branch": link.pull_request.source_branch,
                "base_branch": link.pull_request.base_branch,
                "is_draft": link.pull_request.is_draft,
                "author_login": link.pull_request.author_login,
                "matched_by": link.matched_by,
            }
            for link in links
        ]
        return Response(prs, status=status.HTTP_200_OK)
