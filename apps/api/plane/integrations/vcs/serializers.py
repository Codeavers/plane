# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy

from rest_framework import serializers

from plane.db.models.vcs import VcsConnection, VcsDelivery, VcsProjectSettings, VcsPullRequest
from plane.integrations.vcs.providers.registry import registry
from plane.integrations.vcs.settings import (
    DEFAULT_SETTINGS,
    SettingsError,
    validate_settings as validate_vcs_settings,
)


class VcsConnectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = VcsConnection
        fields = (
            "id",
            "workspace",
            "provider",
            "name",
            "is_active",
            "repo_filter",
            "only_enabled_projects",
            "bot_user",
            "settings",
            "verified_at",
            "last_delivery_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "workspace",
            "bot_user",
            "verified_at",
            "last_delivery_at",
            "created_at",
            "updated_at",
        )

    def validate_provider(self, value):
        if value not in registry:
            raise serializers.ValidationError(f"unknown provider '{value}'")
        return value

    def validate_settings(self, value):
        if not value:
            return copy.deepcopy(DEFAULT_SETTINGS)
        try:
            return validate_vcs_settings(value)
        except SettingsError as e:
            raise serializers.ValidationError(str(e))

    def validate_repo_filter(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("repo_filter must be a list")
        for i, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise serializers.ValidationError(f"repo_filter[{i}] must be a non-empty string")
        return value

    def create(self, validated_data):
        if "settings" not in validated_data or not validated_data["settings"]:
            validated_data["settings"] = copy.deepcopy(DEFAULT_SETTINGS)
        return super().create(validated_data)


class VcsDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = VcsDelivery
        fields = (
            "id",
            "delivery_id",
            "event",
            "action",
            "status",
            "payload",
            "summary",
            "error",
            "attempts",
            "processed_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class VcsProjectSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = VcsProjectSettings
        fields = (
            "id",
            "connection",
            "project",
            "workspace",
            "enabled",
            "overrides",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "project", "workspace", "created_at", "updated_at")

    def validate_overrides(self, value):
        if not value:
            return {}
        try:
            return validate_vcs_settings(value, partial=True)
        except SettingsError as e:
            raise serializers.ValidationError(str(e))


class VcsPullRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = VcsPullRequest
        fields = (
            "id",
            "connection",
            "repo_full_name",
            "number",
            "url",
            "title",
            "source_branch",
            "base_branch",
            "author_login",
            "is_draft",
            "state",
            "merged_at",
            "provider_updated_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields
