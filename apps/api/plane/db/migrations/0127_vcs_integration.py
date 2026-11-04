# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import plane.db.models.api


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0126_issuepage"),
    ]

    operations = [
        migrations.CreateModel(
            name="VcsConnection",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Last Modified At"),
                ),
                (
                    "deleted_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Deleted At"),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                (
                    "provider",
                    models.CharField(max_length=30),
                ),
                (
                    "name",
                    models.CharField(max_length=255),
                ),
                (
                    "secret",
                    models.CharField(
                        default=plane.db.models.api.generate_token,
                        max_length=255,
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(default=True),
                ),
                (
                    "repo_filter",
                    models.JSONField(default=list),
                ),
                (
                    "only_enabled_projects",
                    models.BooleanField(default=False),
                ),
                (
                    "settings",
                    models.JSONField(default=dict),
                ),
                (
                    "verified_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "last_delivery_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "bot_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="bot_vcs_connections",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vcs_connections",
                        to="db.workspace",
                    ),
                ),
            ],
            options={
                "verbose_name": "VCS Connection",
                "verbose_name_plural": "VCS Connections",
                "db_table": "vcs_connections",
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="VcsProjectSettings",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Last Modified At"),
                ),
                (
                    "deleted_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Deleted At"),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(default=True),
                ),
                (
                    "overrides",
                    models.JSONField(default=dict),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="project_settings",
                        to="db.vcsconnection",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="project_%(class)s",
                        to="db.project",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workspace_%(class)s",
                        to="db.workspace",
                    ),
                ),
            ],
            options={
                "verbose_name": "VCS Project Settings",
                "verbose_name_plural": "VCS Project Settings",
                "db_table": "vcs_project_settings",
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="VcsPullRequest",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Last Modified At"),
                ),
                (
                    "deleted_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Deleted At"),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                (
                    "repo_full_name",
                    models.CharField(max_length=512),
                ),
                (
                    "number",
                    models.PositiveIntegerField(),
                ),
                (
                    "url",
                    models.URLField(),
                ),
                (
                    "title",
                    models.TextField(),
                ),
                (
                    "source_branch",
                    models.CharField(max_length=512),
                ),
                (
                    "base_branch",
                    models.CharField(max_length=512),
                ),
                (
                    "author_login",
                    models.CharField(max_length=255),
                ),
                (
                    "is_draft",
                    models.BooleanField(default=False),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("closed", "Closed"),
                            ("merged", "Merged"),
                        ],
                        default="open",
                        max_length=10,
                    ),
                ),
                (
                    "merged_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "provider_updated_at",
                    models.DateTimeField(),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pull_requests",
                        to="db.vcsconnection",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
            ],
            options={
                "verbose_name": "VCS Pull Request",
                "verbose_name_plural": "VCS Pull Requests",
                "db_table": "vcs_pull_requests",
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="VcsPullRequestLink",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Last Modified At"),
                ),
                (
                    "deleted_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Deleted At"),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                (
                    "matched_by",
                    models.CharField(
                        choices=[
                            ("branch", "Branch"),
                            ("title", "Title"),
                            ("body", "Body"),
                        ],
                        max_length=10,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "issue",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vcs_links",
                        to="db.issue",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="project_%(class)s",
                        to="db.project",
                    ),
                ),
                (
                    "pull_request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="links",
                        to="db.vcspullrequest",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workspace_%(class)s",
                        to="db.workspace",
                    ),
                ),
            ],
            options={
                "verbose_name": "VCS Pull Request Link",
                "verbose_name_plural": "VCS Pull Request Links",
                "db_table": "vcs_pull_request_links",
                "ordering": ("-created_at",),
            },
        ),
        migrations.CreateModel(
            name="VcsDelivery",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created At"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Last Modified At"),
                ),
                (
                    "deleted_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Deleted At"),
                ),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                (
                    "delivery_id",
                    models.CharField(max_length=255),
                ),
                (
                    "event",
                    models.CharField(max_length=50),
                ),
                (
                    "action",
                    models.CharField(blank=True, default="", max_length=50),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("received", "Received"),
                            ("processed", "Processed"),
                            ("ignored", "Ignored"),
                            ("failed", "Failed"),
                        ],
                        default="received",
                        max_length=10,
                    ),
                ),
                (
                    "payload",
                    models.JSONField(default=dict),
                ),
                (
                    "summary",
                    models.JSONField(default=dict),
                ),
                (
                    "error",
                    models.TextField(blank=True, default=""),
                ),
                (
                    "attempts",
                    models.PositiveSmallIntegerField(default=0),
                ),
                (
                    "processed_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deliveries",
                        to="db.vcsconnection",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
            ],
            options={
                "verbose_name": "VCS Delivery",
                "verbose_name_plural": "VCS Deliveries",
                "db_table": "vcs_deliveries",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="vcsprojectsettings",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("connection", "project"),
                name="vcs_project_settings_unique_connection_project_when_deleted_at_null",
            ),
        ),
        migrations.AddConstraint(
            model_name="vcspullrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("connection", "repo_full_name", "number"),
                name="vcs_pull_requests_unique_connection_repo_full_name_number_when_deleted_at_null",
            ),
        ),
        migrations.AddConstraint(
            model_name="vcspullrequestlink",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("pull_request", "issue"),
                name="vcs_pull_request_links_unique_pull_request_issue_when_deleted_at_null",
            ),
        ),
        migrations.AddConstraint(
            model_name="vcsdelivery",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("connection", "delivery_id"),
                name="vcs_deliveries_unique_connection_delivery_id_when_deleted_at_null",
            ),
        ),
    ]
