# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import copy
import re
import string

# Guard against ReDoS: simple static check rejects groups quantified with +, * or {
# that themselves contain + or *.
NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*{]")

ALLOWED_ACTIONS = frozenset(
    {
        "opened",
        "reopened",
        "ready_for_review",
        "converted_to_draft",
        "closed",
        "merged",
        "edited",
        "synchronize",
    }
)

ALLOWED_MATCHER_SOURCES = frozenset(
    {
        "branch",
        "title",
        "body",
    }
)

ALLOWED_ACTION_TYPES = frozenset(
    {
        "set_state",
        "comment",
    }
)

ALLOWED_STATE_GROUPS = frozenset(
    {
        "backlog",
        "unstarted",
        "started",
        "completed",
        "cancelled",
    }
)

ALLOWED_TEMPLATE_PLACEHOLDERS = frozenset(
    {
        "url",
        "repo",
        "number",
        "title",
        "base_branch",
        "source_branch",
        "author",
    }
)

ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "matchers",
        "base_branches",
        "rules",
        "comment_templates",
    }
)

DEFAULT_SETTINGS: dict = {
    "version": 1,
    "matchers": [
        {
            "source": "branch",
            "enabled": True,
            "pattern": r"\b([A-Z][A-Z0-9]{1,11})-(\d+)\b",
            "ignore_case": True,
        },
        {
            "source": "title",
            "enabled": True,
            "pattern": r"\b([A-Z][A-Z0-9]{1,11})-(\d+)\b",
        },
        {
            "source": "body",
            "enabled": True,
            "pattern": r"(?i:fixes|closes|resolves|refs)\s+([A-Z][A-Z0-9]{1,11})-(\d+)\b",
        },
    ],
    "base_branches": ["main", "master", "develop", "preview"],
    "rules": [
        {
            "on": ["opened", "reopened", "ready_for_review"],
            "when": {
                "draft": False,
                "from_groups": ["backlog", "unstarted", "started"],
            },
            "actions": [
                {
                    "type": "set_state",
                    "state": "In Review",
                    "fallback_group": "started",
                },
                {"type": "comment", "template": "pr_opened"},
            ],
        },
        {
            "on": ["merged"],
            "when": {
                "base_branch": "base_branches",
                "all_linked_merged": True,
                "from_groups": ["backlog", "unstarted", "started"],
            },
            "actions": [
                {
                    "type": "set_state",
                    "state": "Done",
                    "fallback_group": "completed",
                },
                {"type": "comment", "template": "pr_merged"},
            ],
        },
        {
            "on": ["closed"],
            "when": {"from_groups": ["started"]},
            "actions": [
                {
                    "type": "set_state",
                    "state": "In Progress",
                    "fallback_group": "started",
                },
                {"type": "comment", "template": "pr_closed"},
            ],
        },
    ],
    "comment_templates": {
        "pr_opened": 'PR <a href="{url}">{repo}#{number}</a> opened: {title}',
        "pr_merged": 'PR <a href="{url}">{repo}#{number}</a> merged into {base_branch}',
        "pr_closed": 'PR <a href="{url}">{repo}#{number}</a> closed without merge',
    },
}


class SettingsError(ValueError):
    pass


def compile_pattern(pattern: str, ignore_case: bool = False) -> re.Pattern:
    """Compile and validate a regex pattern for work item identifier extraction.

    Enforces length <= 200, checks against nested quantifiers, and requires
    exactly 2 capturing groups (identifier, sequence_id).
    """
    if not isinstance(pattern, str):
        raise SettingsError("pattern must be a string")

    if len(pattern) > 200:
        raise SettingsError(f"pattern exceeds maximum length of 200 characters: {pattern}")

    if NESTED_QUANTIFIER_RE.search(pattern):
        raise SettingsError(f"pattern contains unsafe nested quantifier: {pattern}")

    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        raise SettingsError(f"invalid regex pattern: {e}")

    if compiled.groups != 2:
        raise SettingsError(f"pattern must have exactly 2 capturing groups, got {compiled.groups}: {pattern}")

    return compiled


def merge_settings(base: dict, override: dict) -> dict:
    """Deep-merge dictionaries and replace lists."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = merge_settings(merged[key], value)
        elif isinstance(value, (dict, list)):
            merged[key] = copy.deepcopy(value)
        else:
            merged[key] = value
    return merged


def validate_settings(data: dict, *, partial: bool = False) -> dict:
    """Validate and normalize VCS integration settings.

    Returns normalized settings dict or raises SettingsError naming the invalid path.
    """
    if not isinstance(data, dict):
        raise SettingsError("settings must be a dictionary")

    for k in data:
        if k not in ALLOWED_TOP_LEVEL_KEYS:
            raise SettingsError(f"unknown setting '{k}'")

    if not partial:
        missing = ALLOWED_TOP_LEVEL_KEYS - set(data.keys())
        if missing:
            raise SettingsError(f"missing required key '{sorted(missing)[0]}'")

    normalized: dict = {}

    if "version" in data:
        version = data["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise SettingsError(f"version: must be 1, got {version!r}")
        normalized["version"] = 1

    if "base_branches" in data:
        base_branches = data["base_branches"]
        if not isinstance(base_branches, list):
            raise SettingsError("base_branches: must be a list")
        norm_branches = []
        for i, branch in enumerate(base_branches):
            if not isinstance(branch, str) or not branch.strip():
                raise SettingsError(f"base_branches[{i}]: must be a non-empty string")
            norm_branches.append(branch)
        normalized["base_branches"] = norm_branches

    if "matchers" in data:
        matchers = data["matchers"]
        if not isinstance(matchers, list):
            raise SettingsError("matchers: must be a list")
        norm_matchers = []
        for i, matcher in enumerate(matchers):
            if not isinstance(matcher, dict):
                raise SettingsError(f"matchers[{i}]: must be a dictionary")
            for k in matcher:
                if k not in {"source", "enabled", "pattern", "ignore_case"}:
                    raise SettingsError(f"matchers[{i}]: unknown key '{k}'")
            if "source" not in matcher:
                raise SettingsError(f"matchers[{i}].source: is required")
            source = matcher["source"]
            if source not in ALLOWED_MATCHER_SOURCES:
                raise SettingsError(f"matchers[{i}].source: unknown source '{source}'")

            if "enabled" not in matcher:
                raise SettingsError(f"matchers[{i}].enabled: is required")
            if not isinstance(matcher["enabled"], bool):
                raise SettingsError(f"matchers[{i}].enabled: must be a boolean")

            if "pattern" not in matcher:
                raise SettingsError(f"matchers[{i}].pattern: is required")
            pattern = matcher["pattern"]
            if not isinstance(pattern, str):
                raise SettingsError(f"matchers[{i}].pattern: must be a string")

            if "ignore_case" in matcher and not isinstance(matcher["ignore_case"], bool):
                raise SettingsError(f"matchers[{i}].ignore_case: must be a boolean")

            ignore_case = matcher.get("ignore_case", False)
            try:
                compile_pattern(pattern, ignore_case=ignore_case)
            except SettingsError as e:
                raise SettingsError(f"matchers[{i}].pattern: {e}")

            norm_m = {
                "source": source,
                "enabled": matcher["enabled"],
                "pattern": pattern,
            }
            if "ignore_case" in matcher:
                norm_m["ignore_case"] = matcher["ignore_case"]
            norm_matchers.append(norm_m)
        normalized["matchers"] = norm_matchers

    if "rules" in data:
        rules = data["rules"]
        if not isinstance(rules, list):
            raise SettingsError("rules: must be a list")
        norm_rules = []
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise SettingsError(f"rules[{i}]: must be a dictionary")
            for k in rule:
                if k not in {"on", "when", "actions"}:
                    raise SettingsError(f"rules[{i}]: unknown key '{k}'")

            if "on" not in rule:
                raise SettingsError(f"rules[{i}].on: is required")
            if not isinstance(rule["on"], list):
                raise SettingsError(f"rules[{i}].on: must be a list")
            if len(rule["on"]) == 0:
                raise SettingsError(f"rules[{i}].on: cannot be empty")
            norm_on = []
            for action in rule["on"]:
                if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
                    raise SettingsError(f"rules[{i}].on: unknown action '{action}'")
                norm_on.append(action)
            norm_rule = {"on": norm_on}

            if "when" in rule:
                when = rule["when"]
                if not isinstance(when, dict):
                    raise SettingsError(f"rules[{i}].when: must be a dictionary")
                for k in when:
                    if k not in {"draft", "all_linked_merged", "from_groups", "base_branch"}:
                        raise SettingsError(f"rules[{i}].when: unknown condition '{k}'")
                norm_when = {}
                if "draft" in when:
                    if not isinstance(when["draft"], bool):
                        raise SettingsError(f"rules[{i}].when.draft: must be a boolean")
                    norm_when["draft"] = when["draft"]
                if "all_linked_merged" in when:
                    if not isinstance(when["all_linked_merged"], bool):
                        raise SettingsError(f"rules[{i}].when.all_linked_merged: must be a boolean")
                    norm_when["all_linked_merged"] = when["all_linked_merged"]
                if "from_groups" in when:
                    from_groups = when["from_groups"]
                    if not isinstance(from_groups, list):
                        raise SettingsError(f"rules[{i}].when.from_groups: must be a list")
                    norm_from_groups = []
                    for group in from_groups:
                        if not isinstance(group, str) or group not in ALLOWED_STATE_GROUPS:
                            raise SettingsError(f"rules[{i}].when.from_groups: unknown state group '{group}'")
                        norm_from_groups.append(group)
                    norm_when["from_groups"] = norm_from_groups
                if "base_branch" in when:
                    base_branch = when["base_branch"]
                    if base_branch == "base_branches":
                        norm_when["base_branch"] = "base_branches"
                    elif isinstance(base_branch, list) and all(isinstance(b, str) and b.strip() for b in base_branch):
                        norm_when["base_branch"] = list(base_branch)
                    else:
                        raise SettingsError(
                            f"rules[{i}].when.base_branch: must be 'base_branches' or a list of branch names"
                        )
                norm_rule["when"] = norm_when

            if "actions" not in rule:
                raise SettingsError(f"rules[{i}].actions: is required")
            if not isinstance(rule["actions"], list):
                raise SettingsError(f"rules[{i}].actions: must be a list")
            if len(rule["actions"]) == 0:
                raise SettingsError(f"rules[{i}].actions: cannot be empty")
            norm_actions = []
            for j, act in enumerate(rule["actions"]):
                if not isinstance(act, dict):
                    raise SettingsError(f"rules[{i}].actions[{j}]: must be a dictionary")
                if "type" not in act:
                    raise SettingsError(f"rules[{i}].actions[{j}].type: is required")
                act_type = act["type"]
                if act_type not in ALLOWED_ACTION_TYPES:
                    raise SettingsError(f"rules[{i}].actions[{j}].type: unknown action type '{act_type}'")

                if act_type == "set_state":
                    for k in act:
                        if k not in {"type", "state", "fallback_group"}:
                            raise SettingsError(f"rules[{i}].actions[{j}]: unknown key '{k}'")
                    if "state" not in act or not isinstance(act["state"], str) or not act["state"].strip():
                        raise SettingsError(f"rules[{i}].actions[{j}].state: must be a non-empty string")
                    norm_act = {"type": "set_state", "state": act["state"]}
                    if "fallback_group" in act:
                        fg = act["fallback_group"]
                        if not isinstance(fg, str) or fg not in ALLOWED_STATE_GROUPS:
                            raise SettingsError(f"rules[{i}].actions[{j}].fallback_group: unknown state group '{fg}'")
                        norm_act["fallback_group"] = fg
                    norm_actions.append(norm_act)
                elif act_type == "comment":
                    for k in act:
                        if k not in {"type", "template"}:
                            raise SettingsError(f"rules[{i}].actions[{j}]: unknown key '{k}'")
                    if "template" not in act or not isinstance(act["template"], str) or not act["template"].strip():
                        raise SettingsError(f"rules[{i}].actions[{j}].template: must be a non-empty string")
                    norm_actions.append({"type": "comment", "template": act["template"]})
            norm_rule["actions"] = norm_actions
            norm_rules.append(norm_rule)
        normalized["rules"] = norm_rules

    if "comment_templates" in data:
        templates = data["comment_templates"]
        if not isinstance(templates, dict):
            raise SettingsError("comment_templates: must be a dictionary")
        norm_templates = {}
        for name, content in templates.items():
            if not isinstance(name, str) or not name.strip():
                raise SettingsError("comment_templates: template names must be non-empty strings")
            if not isinstance(content, str):
                raise SettingsError(f"comment_templates.{name}: must be a string")
            try:
                parsed = list(string.Formatter().parse(content))
            except ValueError as e:
                raise SettingsError(f"comment_templates.{name}: invalid format string: {e}")
            for _, field_name, _, _ in parsed:
                if field_name is not None:
                    if field_name not in ALLOWED_TEMPLATE_PLACEHOLDERS:
                        raise SettingsError(f"comment_templates.{name}: unknown placeholder '{field_name}'")
            norm_templates[name] = content
        normalized["comment_templates"] = norm_templates

    return normalized
