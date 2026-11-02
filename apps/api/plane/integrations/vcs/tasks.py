# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

from celery import Task, shared_task
from django.db import InterfaceError, OperationalError
from django.utils import timezone

from plane.db.models.vcs import VcsDelivery
from plane.integrations.vcs.service import process_delivery


class VcsDeliveryTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        if args:
            delivery_id = args[0]
            try:
                VcsDelivery.objects.filter(pk=delivery_id).update(
                    status=VcsDelivery.STATUS_FAILED,
                    error=str(exc),
                    processed_at=timezone.now(),
                )
            except Exception:
                pass
        super().on_failure(exc, task_id, args, kwargs, einfo)


@shared_task(
    bind=True,
    base=VcsDeliveryTask,
    autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True,
    max_retries=3,
    soft_time_limit=60,
    time_limit=90,
)
def process_vcs_delivery(self, delivery_id: str) -> None:
    try:
        delivery = VcsDelivery.objects.get(pk=delivery_id)
        process_delivery(delivery)
    except (OperationalError, InterfaceError) as exc:
        if self.request.retries >= self.max_retries:
            try:
                VcsDelivery.objects.filter(pk=delivery_id).update(
                    status=VcsDelivery.STATUS_FAILED,
                    error=str(exc),
                    processed_at=timezone.now(),
                )
            except Exception:
                pass
        raise


@shared_task
def purge_vcs_deliveries() -> None:
    cutoff = timezone.now() - timedelta(days=30)
    VcsDelivery.all_objects.filter(created_at__lt=cutoff).delete()
