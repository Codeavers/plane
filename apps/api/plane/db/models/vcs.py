# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.conf import settings
from django.db import models
from django.db.models import Q

from plane.db.models.api import generate_token
from plane.db.models.base import BaseModel
from plane.db.models.project import ProjectBaseModel


class VcsConnection(BaseModel):
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="vcs_connections",
    )
    provider = models.CharField(max_length=30)
    name = models.CharField(max_length=255)
    secret = models.CharField(max_length=255, default=generate_token)
    is_active = models.BooleanField(default=True)
    repo_filter = models.JSONField(default=list)
    only_enabled_projects = models.BooleanField(default=False)
    bot_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bot_vcs_connections",
    )
    settings = models.JSONField(default=dict)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_delivery_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "VCS Connection"
        verbose_name_plural = "VCS Connections"
        db_table = "vcs_connections"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.name} ({self.provider})"


class VcsProjectSettings(ProjectBaseModel):
    connection = models.ForeignKey(
        "db.VcsConnection",
        on_delete=models.CASCADE,
        related_name="project_settings",
    )
    enabled = models.BooleanField(default=True)
    overrides = models.JSONField(default=dict)

    class Meta:
        verbose_name = "VCS Project Settings"
        verbose_name_plural = "VCS Project Settings"
        db_table = "vcs_project_settings"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "project"],
                condition=Q(deleted_at__isnull=True),
                name="vcs_project_settings_unique_connection_project_when_deleted_at_null",
            )
        ]

    def __str__(self):
        return f"{self.project} - {self.connection}"


class VcsPullRequest(BaseModel):
    STATE_OPEN = "open"
    STATE_CLOSED = "closed"
    STATE_MERGED = "merged"

    STATE_CHOICES = (
        (STATE_OPEN, "Open"),
        (STATE_CLOSED, "Closed"),
        (STATE_MERGED, "Merged"),
    )

    connection = models.ForeignKey(
        "db.VcsConnection",
        on_delete=models.CASCADE,
        related_name="pull_requests",
    )
    repo_full_name = models.CharField(max_length=512)
    number = models.PositiveIntegerField()
    url = models.URLField()
    title = models.TextField()
    source_branch = models.CharField(max_length=512)
    base_branch = models.CharField(max_length=512)
    author_login = models.CharField(max_length=255)
    is_draft = models.BooleanField(default=False)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=STATE_OPEN)
    merged_at = models.DateTimeField(null=True, blank=True)
    provider_updated_at = models.DateTimeField()

    class Meta:
        verbose_name = "VCS Pull Request"
        verbose_name_plural = "VCS Pull Requests"
        db_table = "vcs_pull_requests"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "repo_full_name", "number"],
                condition=Q(deleted_at__isnull=True),
                name="vcs_pull_requests_unique_connection_repo_full_name_number_when_deleted_at_null",
            )
        ]

    def __str__(self):
        return f"{self.repo_full_name}#{self.number}"


class VcsPullRequestLink(ProjectBaseModel):
    MATCHED_BY_BRANCH = "branch"
    MATCHED_BY_TITLE = "title"
    MATCHED_BY_BODY = "body"

    MATCHED_BY = (
        (MATCHED_BY_BRANCH, "Branch"),
        (MATCHED_BY_TITLE, "Title"),
        (MATCHED_BY_BODY, "Body"),
    )

    pull_request = models.ForeignKey(
        "db.VcsPullRequest",
        on_delete=models.CASCADE,
        related_name="links",
    )
    issue = models.ForeignKey(
        "db.Issue",
        on_delete=models.CASCADE,
        related_name="vcs_links",
    )
    matched_by = models.CharField(max_length=10, choices=MATCHED_BY)

    class Meta:
        verbose_name = "VCS Pull Request Link"
        verbose_name_plural = "VCS Pull Request Links"
        db_table = "vcs_pull_request_links"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["pull_request", "issue"],
                condition=Q(deleted_at__isnull=True),
                name="vcs_pull_request_links_unique_pull_request_issue_when_deleted_at_null",
            )
        ]

    def save(self, *args, **kwargs):
        if not self.project_id and self.issue_id:
            self.project = self.issue.project
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.pull_request} -> {self.issue}"


class VcsDelivery(BaseModel):
    STATUS_RECEIVED = "received"
    STATUS_PROCESSED = "processed"
    STATUS_IGNORED = "ignored"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = (
        (STATUS_RECEIVED, "Received"),
        (STATUS_PROCESSED, "Processed"),
        (STATUS_IGNORED, "Ignored"),
        (STATUS_FAILED, "Failed"),
    )

    connection = models.ForeignKey(
        "db.VcsConnection",
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    delivery_id = models.CharField(max_length=255)
    event = models.CharField(max_length=50)
    action = models.CharField(max_length=50, blank=True, default="")
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default=STATUS_RECEIVED,
    )
    payload = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "VCS Delivery"
        verbose_name_plural = "VCS Deliveries"
        db_table = "vcs_deliveries"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "delivery_id"],
                condition=Q(deleted_at__isnull=True),
                name="vcs_deliveries_unique_connection_delivery_id_when_deleted_at_null",
            )
        ]

    def __str__(self):
        return f"{self.connection} - {self.delivery_id} ({self.status})"


# Module-level aliases for convenience
STATE_OPEN = VcsPullRequest.STATE_OPEN
STATE_CLOSED = VcsPullRequest.STATE_CLOSED
STATE_MERGED = VcsPullRequest.STATE_MERGED

STATUS_RECEIVED = VcsDelivery.STATUS_RECEIVED
STATUS_PROCESSED = VcsDelivery.STATUS_PROCESSED
STATUS_IGNORED = VcsDelivery.STATUS_IGNORED
STATUS_FAILED = VcsDelivery.STATUS_FAILED

MATCHED_BY = VcsPullRequestLink.MATCHED_BY
