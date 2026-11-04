# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from plane.db.models.vcs import VcsConnection, VcsDelivery
from plane.integrations.vcs.providers.registry import registry
from plane.integrations.vcs.tasks import process_vcs_delivery


class VcsWebhookThrottle(ScopedRateThrottle):
    scope = "vcs_webhook"
    THROTTLE_RATES = {"vcs_webhook": "600/min"}

    def get_cache_key(self, request, view):
        connection_id = view.kwargs.get("connection_id") if hasattr(view, "kwargs") else None
        if not connection_id:
            return None
        return self.cache_format % {
            "scope": self.scope,
            "ident": str(connection_id),
        }


class VcsWebhookEndpoint(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [VcsWebhookThrottle]
    throttle_scope = "vcs_webhook"

    def post(self, request, provider: str, connection_id: str, *args, **kwargs) -> Response:
        try:
            connection = VcsConnection.objects.get(pk=connection_id)
        except (VcsConnection.DoesNotExist, ValueError):
            return Response({}, status=status.HTTP_404_NOT_FOUND)

        if not connection.is_active:
            return Response({}, status=status.HTTP_404_NOT_FOUND)

        if connection.provider != provider or provider not in registry:
            return Response({}, status=status.HTTP_404_NOT_FOUND)

        vcs_provider = registry.get(provider)

        raw_body = request.body
        if not vcs_provider.verify(request.headers, raw_body, connection.secret):
            return Response({}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return Response({"error": "body must be application/json"}, status=status.HTTP_400_BAD_REQUEST)

        delivery_id = vcs_provider.delivery_id(request.headers)
        if not delivery_id:
            return Response({"error": "missing delivery id"}, status=status.HTTP_400_BAD_REQUEST)

        if VcsDelivery.objects.filter(connection=connection, delivery_id=delivery_id).exists():
            return Response({"status": "duplicate"}, status=status.HTTP_200_OK)

        trimmed = vcs_provider.trim_payload(request.headers, payload)
        event = vcs_provider.event_name(request.headers)
        action = payload.get("action", "")

        with transaction.atomic():
            delivery, created = VcsDelivery.objects.get_or_create(
                connection=connection,
                delivery_id=delivery_id,
                defaults={
                    "event": event,
                    "action": action,
                    "status": VcsDelivery.STATUS_RECEIVED,
                    "payload": trimmed,
                },
            )
            if not created:
                return Response({"status": "duplicate"}, status=status.HTTP_200_OK)

            connection.last_delivery_at = timezone.now()
            connection.save(update_fields=["last_delivery_at", "updated_at"])

            transaction.on_commit(lambda: process_vcs_delivery.delay(str(delivery.id)))

        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)
