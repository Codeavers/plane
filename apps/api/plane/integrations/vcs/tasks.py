# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

from celery import Task, shared_task
from django.db import InterfaceError, OperationalError
from django.utils import timezone

from plane.db.models.vcs import VcsDelivery
from plane.integrations.vcs.service import process_delivery
from plane.utils.exception_logger import log_exception


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
            except Exception as e:
                log_exception(e)
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
    delivery = VcsDelivery.objects.get(pk=delivery_id)
    process_delivery(delivery)


@shared_task
def purge_vcs_deliveries() -> None:
    cutoff = timezone.now() - timedelta(days=30)
    VcsDelivery.all_objects.filter(created_at__lt=cutoff).delete()
