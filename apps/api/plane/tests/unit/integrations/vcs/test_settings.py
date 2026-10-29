# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
import re

import pytest

from plane.integrations.vcs.settings import (
    DEFAULT_SETTINGS,
    SettingsError,
    compile_pattern,
    merge_settings,
    validate_settings,
)


@pytest.mark.unit
class TestVcsSettingsValidation:
    def test_default_settings_are_valid(self):
        validated = validate_settings(DEFAULT_SETTINGS)
        assert validated == DEFAULT_SETTINGS
        assert validated["version"] == 1
        branch_matcher = next(m for m in validated["matchers"] if m["source"] == "branch")
        assert branch_matcher.get("ignore_case") is True

    def test_unknown_rule_action_rejected(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["rules"][0]["on"] = ["invalid_action"]
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "rules[0].on" in str(exc_info.value)
        assert "invalid_action" in str(exc_info.value)

    def test_pattern_must_have_two_groups(self):
        # 1 group
        with pytest.raises(SettingsError) as exc_info:
            compile_pattern(r"([A-Z]+)-\d+")
        assert "2 capturing groups" in str(exc_info.value)

        # 3 groups
        with pytest.raises(SettingsError) as exc_info:
            compile_pattern(r"([A-Z]+)-(\d+)-([a-z]+)")
        assert "2 capturing groups" in str(exc_info.value)

        # 0 groups
        with pytest.raises(SettingsError) as exc_info:
            compile_pattern(r"[A-Z]+-\d+")
        assert "2 capturing groups" in str(exc_info.value)

        # Non-capturing group with 2 capturing groups is allowed
        compiled = compile_pattern(r"(?i:fixes|closes|resolves|refs)\s+([A-Z][A-Z0-9]{1,11})-(\d+)\b")
        assert compiled.groups == 2

    def test_rejects_nested_quantifier_pattern(self):
        with pytest.raises(SettingsError) as exc_info:
            compile_pattern(r"(a+)+-(\d+)")
        assert "nested quantifier" in str(exc_info.value)

        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["matchers"][0]["pattern"] = r"(a+)+-(\d+)"
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "matchers[0].pattern" in str(exc_info.value)

    def test_rejects_pattern_longer_than_200(self):
        long_pattern = "(" + "a" * 195 + r")-(\d+)"
        assert len(long_pattern) > 200
        with pytest.raises(SettingsError) as exc_info:
            compile_pattern(long_pattern)
        assert "200" in str(exc_info.value)

        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["matchers"][0]["pattern"] = long_pattern
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "matchers[0].pattern" in str(exc_info.value)

    def test_unknown_template_placeholder_rejected(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["comment_templates"]["pr_opened"] = "PR {secret} opened"
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "secret" in str(exc_info.value)
        assert "comment_templates.pr_opened" in str(exc_info.value)

    def test_partial_override_validates_only_present_keys(self):
        # Empty dict is a valid partial override
        assert validate_settings({}, partial=True) == {}

        # Valid partial override
        override = {
            "base_branches": ["release/*"],
            "comment_templates": {"pr_opened": 'PR <a href="{url}">{repo}#{number}</a> opened: {title}'},
        }
        res = validate_settings(override, partial=True)
        assert res == override

        # Partial override with invalid value fails
        with pytest.raises(SettingsError) as exc_info:
            validate_settings({"base_branches": [123]}, partial=True)
        assert "base_branches[0]" in str(exc_info.value)

        # Partial override with unknown key fails
        with pytest.raises(SettingsError) as exc_info:
            validate_settings({"unknown_field": True}, partial=True)
        assert "unknown setting 'unknown_field'" in str(exc_info.value)

    def test_merge_replaces_lists_and_merges_dicts(self):
        override = {
            "base_branches": ["release/*"],
            "comment_templates": {"pr_opened": "x"},
        }
        merged = merge_settings(DEFAULT_SETTINGS, override)

        # Lists are replaced
        assert merged["base_branches"] == ["release/*"]

        # Dicts are deep-merged (pr_opened overridden, pr_merged and pr_closed preserved)
        assert merged["comment_templates"]["pr_opened"] == "x"
        assert merged["comment_templates"]["pr_merged"] == DEFAULT_SETTINGS["comment_templates"]["pr_merged"]
        assert merged["comment_templates"]["pr_closed"] == DEFAULT_SETTINGS["comment_templates"]["pr_closed"]

        # Other keys preserved
        assert merged["version"] == DEFAULT_SETTINGS["version"]
        assert merged["matchers"] == DEFAULT_SETTINGS["matchers"]
        assert merged["rules"] == DEFAULT_SETTINGS["rules"]

        # Base and override not mutated
        assert DEFAULT_SETTINGS["base_branches"] != ["release/*"]
        assert DEFAULT_SETTINGS["comment_templates"]["pr_opened"] != "x"

    def test_compile_pattern_ignore_case(self):
        compiled = compile_pattern(r"\b([A-Z]+)-(\d+)\b", ignore_case=True)
        assert bool(compiled.flags & re.IGNORECASE)
        match = compiled.search("feat/codea-40-foo")
        assert match is not None
        assert match.groups() == ("codea", "40")

    def test_compile_pattern_invalid_regex(self):
        with pytest.raises(SettingsError) as exc_info:
            compile_pattern(r"([A-Z]+-(\d+")
        assert "invalid regex" in str(exc_info.value).lower()

    def test_validate_settings_not_a_dict(self):
        with pytest.raises(SettingsError) as exc_info:
            validate_settings("not a dict")
        assert "must be a dictionary" in str(exc_info.value)

    def test_missing_required_key_in_full_settings(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        del data["rules"]
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data, partial=False)
        assert "missing required key 'rules'" in str(exc_info.value)

    def test_invalid_version_rejected(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["version"] = 2
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "version" in str(exc_info.value)

    def test_invalid_matcher_source_rejected(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["matchers"][0]["source"] = "commit_message"
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "matchers[0].source" in str(exc_info.value)

    def test_invalid_action_type_rejected(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["rules"][0]["actions"][0]["type"] = "send_email"
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "rules[0].actions[0].type" in str(exc_info.value)

    def test_invalid_state_group_in_when_rejected(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["rules"][0]["when"]["from_groups"] = ["invalid_group"]
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "rules[0].when.from_groups" in str(exc_info.value)

    def test_invalid_fallback_group_rejected(self):
        data = copy.deepcopy(DEFAULT_SETTINGS)
        data["rules"][0]["actions"][0]["fallback_group"] = "invalid_group"
        with pytest.raises(SettingsError) as exc_info:
            validate_settings(data)
        assert "rules[0].actions[0].fallback_group" in str(exc_info.value)
