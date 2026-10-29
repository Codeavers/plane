# VCS Integration Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull request webhooks from GitHub move the referenced Plane work items through their workflow (In Review, Done, back to In Progress) with link comments, configurable per workspace and project, behind a provider-agnostic core.

**Architecture:** Models live in `plane/db/models/vcs.py` (the `db` app owns every model and migration). The engine is a plain package `plane/integrations/vcs/`: provider adapters (Strategy + registry) normalize payloads into a `PullRequestEvent`; matchers extract work item ids; a rule engine selects command actions; a service orchestrates one delivery inside a Celery task. A signature-authenticated webhook view stores the delivery and enqueues it. Configuration endpoints live in the app API.

**Tech Stack:** Django 4 / DRF, Celery, PostgreSQL, pytest (`docker-compose-test.yml`), ruff 0.9.7.

**Spec:** `docs/superpowers/specs/2026-10-29-vcs-integration-core-design.md`

## Global Constraints

- Every new `.py` file starts with the 3-line header: `# Copyright (c) 2023-present Plane Software, Inc. and contributors` / `# SPDX-License-Identifier: AGPL-3.0-only` / `# See the LICENSE file for details.`
- Table names prefixed `vcs_`. Models extend `BaseModel` / `ProjectBaseModel` from `plane.db.models.base` / `plane.db.models.project`.
- Migration: `apps/api/plane/db/migrations/0126_vcs_integration.py`, depends on `("db", "0125_reset_project_deploy_board_identity")`. Generate it with `python manage.py makemigrations db -n vcs_integration` inside the test container; `makemigrations db --check` must report no changes afterwards. (If upstream lands another `0126` first, renumber at rebase time.)
- Provider key `"github"`. Bot user: `is_bot=True`, `bot_type="VCS"`.
- Delivery retention: 30 days. Celery retries: 3, exponential backoff.
- Default settings JSON is copied verbatim from the spec "Settings schema (version 1)" section, with one change decided in this plan: the `branch` matcher is case-insensitive (see Review Focus 1).
- No new third-party dependencies.
- Code comments only for why/constraints. Lint: `uvx ruff@0.9.7 format` and `uvx ruff@0.9.7 check` on changed files.
- Tests run from the worktree root: `docker compose -f docker-compose-test.yml run --rm --build api-tests pytest <paths> -q`; full suite `docker compose -f docker-compose-test.yml up --build --abort-on-container-exit --exit-code-from api-tests`; always `docker compose -f docker-compose-test.yml down -v` afterwards.
- Unit tests: `apps/api/plane/tests/unit/integrations/vcs/` marked `@pytest.mark.unit`. DB tests: `apps/api/plane/tests/contract/app/vcs/` marked `@pytest.mark.contract` + `@pytest.mark.django_db`. Celery runs eager in DB tests via `settings.CELERY_TASK_ALWAYS_EAGER = True` (pytest `settings` fixture).

## Review Focus

1. **Lowercase ids in branch names** (`feat/codea-40-github-sync`): a person expects the branch to link CODEA-40. The branch matcher compiles with `re.IGNORECASE` and identifiers are upper-cased before resolving. Test in Task 4: `test_branch_matcher_is_case_insensitive`.
2. **An id added to a PR after it was opened** (title edited to include CODEA-41): a person expects CODEA-41 to move to In Review like the others. Decision: links created by any event on an open, non-draft PR are evaluated with the rules for `opened`. Test in Task 6: `test_link_added_by_edit_on_open_pr_applies_opened_rules`.
3. **Pathological regex in a configured matcher** (`(a+)+$`): a person expects the save to be rejected, not the worker to hang. Validation compiles the pattern, requires exactly 2 groups, limits length to 200 and rejects nested quantifiers. Test in Task 2: `test_rejects_nested_quantifier_pattern`.
4. **Text that looks like an id but is not a work item** (`UTF-8`, `SHA-256` in a PR body): a person expects nothing to happen and no error. Unresolved ids are dropped silently and recorded in the summary. Test in Task 6: `test_unresolved_identifiers_are_ignored`.
5. **Large payloads** (PR body of several MB): a person expects the delivery to be processed. The stored payload is trimmed to the fields the adapter reads, and the body text is capped at 65 536 chars before matching. Test in Task 3: `test_parse_caps_body_length`.

---

### Task 1: Models and migration

**Files:**
- Create: `apps/api/plane/db/models/vcs.py`
- Modify: `apps/api/plane/db/models/__init__.py` (export the five models)
- Create: `apps/api/plane/db/migrations/0126_vcs_integration.py` (generated)
- Test: `apps/api/plane/tests/contract/app/vcs/test_models.py`

**Interfaces:**
- Produces: `VcsConnection`, `VcsProjectSettings`, `VcsPullRequest`, `VcsPullRequestLink`, `VcsDelivery` with the fields, types and unique constraints of the spec "Data model" section. Choices as module constants: `VcsPullRequest.STATE_OPEN = "open"`, `STATE_CLOSED = "closed"`, `STATE_MERGED = "merged"`; `VcsDelivery.STATUS_RECEIVED/PROCESSED/IGNORED/FAILED` = `"received"`, `"processed"`, `"ignored"`, `"failed"`; `VcsPullRequestLink.MATCHED_BY` choices `branch`, `title`, `body`. `VcsConnection.secret` default = `plane.db.models.api.generate_token`. `related_name`s: connection → `project_settings`, `pull_requests`, `deliveries`; pull_request → `links`; issue → `vcs_links`.

- [ ] **Step 1: Write failing tests** `test_pull_request_unique_per_connection_repo_number` (second create with same connection/repo/number raises `IntegrityError`), `test_link_unique_per_pull_request_and_issue`, `test_delivery_unique_per_connection_and_delivery_id`, `test_soft_deleted_link_allows_relink` (delete then create same pair succeeds), `test_connection_secret_is_generated` (non-empty, two connections differ).
- [ ] **Step 2: Run** `pytest plane/tests/contract/app/vcs/test_models.py -q` → fails on import.
- [ ] **Step 3: Implement the models**; partial unique constraints `condition=Q(deleted_at__isnull=True)` like `ProjectIssueType`; `db_table` `vcs_connections`, `vcs_project_settings`, `vcs_pull_requests`, `vcs_pull_request_links`, `vcs_deliveries`.
- [ ] **Step 4: Generate the migration** in the container (`python manage.py makemigrations db -n vcs_integration`), copy it out, run `makemigrations db --check --dry-run` → "No changes detected".
- [ ] **Step 5: Run tests** → pass.
- [ ] **Step 6: Commit** `feat(vcs): add VCS integration models`.

### Task 2: Settings schema, validation and merge

**Files:**
- Create: `apps/api/plane/integrations/__init__.py`, `apps/api/plane/integrations/vcs/__init__.py`, `apps/api/plane/integrations/vcs/settings.py`
- Test: `apps/api/plane/tests/unit/integrations/vcs/test_settings.py`

**Interfaces:**
- Produces: `DEFAULT_SETTINGS: dict` (spec JSON, branch matcher flagged `"ignore_case": true`); `class SettingsError(ValueError)`; `validate_settings(data: dict, *, partial: bool = False) -> dict` (returns normalized data or raises `SettingsError` with a message naming the path, e.g. `"rules[1].on: unknown action 'foo'"`); `merge_settings(base: dict, override: dict) -> dict` (deep-merge dicts, replace lists); `compile_pattern(pattern: str, ignore_case: bool = False) -> re.Pattern`.
- Allowed values: actions `opened`, `reopened`, `ready_for_review`, `converted_to_draft`, `closed`, `merged`, `edited`, `synchronize`; matcher sources `branch`, `title`, `body`; action types `set_state`, `comment`; state groups `backlog`, `unstarted`, `started`, `completed`, `cancelled`; template placeholders `url`, `repo`, `number`, `title`, `base_branch`, `source_branch`, `author`.

- [ ] **Step 1: Write failing tests:** `test_default_settings_are_valid`; `test_unknown_rule_action_rejected` (message contains `rules[0].on`); `test_pattern_must_have_two_groups`; `test_rejects_nested_quantifier_pattern` (`"(a+)+-(\\d+)"` → `SettingsError`); `test_rejects_pattern_longer_than_200`; `test_unknown_template_placeholder_rejected` (`"{secret}"`); `test_partial_override_validates_only_present_keys`; `test_merge_replaces_lists_and_merges_dicts` (override `{"base_branches": ["release/*"], "comment_templates": {"pr_opened": "x"}}` keeps the other templates and replaces the branch list).
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement.** Nested-quantifier check: reject when a group containing `+` or `*` is itself followed by `+`, `*` or `{` (simple regex over the pattern source is enough).
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(vcs): settings schema, validation and merge`.

### Task 3: Events, provider interface, registry and GitHub adapter

**Files:**
- Create: `apps/api/plane/integrations/vcs/events.py`, `providers/__init__.py`, `providers/base.py`, `providers/registry.py`, `providers/github.py`
- Create fixtures: `apps/api/plane/tests/unit/integrations/vcs/fixtures/github/*.json` (opened, opened_draft, ready_for_review, reopened, closed_merged, closed_unmerged, edited, synchronize, ping, issues_event). Shape from GitHub's documented `pull_request` webhook payload; keep only realistic fields the adapter reads plus a few extra.
- Test: `apps/api/plane/tests/unit/integrations/vcs/test_github_provider.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) PullRequestEvent(provider: str, repo_full_name: str, number: int, url: str, title: str, body: str, source_branch: str, base_branch: str, action: str, is_draft: bool, author_login: str, updated_at: datetime, merged_at: datetime | None)`; `@dataclass(frozen=True) PingEvent(provider: str)`.
- `class VcsProvider(ABC)`: `key: str`; `verify(headers: Mapping[str, str], raw_body: bytes, secret: str) -> bool`; `delivery_id(headers) -> str | None`; `event_name(headers) -> str`; `parse(headers, payload: dict) -> PullRequestEvent | PingEvent | None`; `trim_payload(headers, payload: dict) -> dict`.
- `registry.register(provider: VcsProvider) -> None`, `registry.get(key: str) -> VcsProvider` (raises `KeyError`), `registry.all() -> list[VcsProvider]`. `providers/__init__.py` registers `GitHubProvider()` on import.
- GitHub specifics: headers `X-Hub-Signature-256` (`sha256=<hex>`, `hmac.compare_digest`), `X-GitHub-Delivery`, `X-GitHub-Event`; `closed` with `pull_request.merged == true` → action `merged`; events other than `pull_request` and `ping` → `None`; body `None` → `""`; body capped at 65 536 chars.

- [ ] **Step 1: Write failing tests:** `test_verify_accepts_valid_signature`, `test_verify_rejects_wrong_secret`, `test_verify_rejects_missing_header`, `test_verify_rejects_sha1_header`, `test_parse_opened`, `test_parse_draft_flag`, `test_parse_closed_merged_maps_to_merged` (and `merged_at` set), `test_parse_closed_unmerged_stays_closed`, `test_parse_ping`, `test_parse_ignores_other_events` (`issues` → `None`), `test_parse_caps_body_length`, `test_trim_payload_keeps_only_read_fields`, `test_registry_returns_github`.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(vcs): provider interface and GitHub adapter`.

### Task 4: Matchers and work item resolution

**Files:**
- Create: `apps/api/plane/integrations/vcs/matchers.py`
- Test: `apps/api/plane/tests/unit/integrations/vcs/test_matchers.py`, `apps/api/plane/tests/contract/app/vcs/test_resolve.py`

**Interfaces:**
- Consumes: `compile_pattern` (Task 2), `PullRequestEvent` (Task 3).
- Produces: `@dataclass(frozen=True) MatchedId(identifier: str, sequence_id: int, source: str)`; `extract_ids(event: PullRequestEvent, matchers: list[dict]) -> list[MatchedId]` (enabled matchers in order, identifier upper-cased, deduplicated by `(identifier, sequence_id)` keeping the first source); `resolve_work_items(workspace_id, ids: list[MatchedId]) -> list[tuple[Issue, MatchedId]]` (uses `Issue.issue_objects` filtered by `workspace_id`, `project__identifier`, `sequence_id`; excludes archived projects; unresolved ids omitted).

- [ ] **Step 1: Write failing unit tests:** `test_extracts_from_branch_title_body`, `test_branch_matcher_is_case_insensitive` (`feat/codea-40-x` → `CODEA`, 40), `test_disabled_matcher_skipped`, `test_duplicates_keep_first_source`, `test_body_requires_keyword_by_default` (`"see CODEA-1"` not matched, `"Fixes CODEA-1"` matched).
- [ ] **Step 2: Write failing DB tests:** `test_resolves_by_identifier_and_sequence`, `test_does_not_resolve_other_workspace` (same identifier in another workspace untouched), `test_unresolved_ids_omitted`.
- [ ] **Step 3: Run** → fail. **Step 4: Implement.** **Step 5: Run** → pass.
- [ ] **Step 6: Commit** `feat(vcs): id matchers and work item resolution`.

### Task 5: Rule engine and actions

**Files:**
- Create: `apps/api/plane/integrations/vcs/rules.py`, `apps/api/plane/integrations/vcs/actions.py`
- Test: `apps/api/plane/tests/unit/integrations/vcs/test_rules.py`, `apps/api/plane/tests/contract/app/vcs/test_actions.py`

**Interfaces:**
- Consumes: settings dict (Task 2), `PullRequestEvent` (Task 3).
- Produces:
  - `@dataclass(frozen=True) RuleContext(event: PullRequestEvent, current_state_group: str, all_linked_merged: bool, base_branches: list[str])`; `select_actions(settings: dict, ctx: RuleContext, action: str | None = None) -> list[dict]` (`action` overrides `ctx.event.action`, used by Task 6 for newly linked work items; base branch globs via `fnmatch`; rules evaluated in order, all matching rules contribute actions).
  - `@dataclass ActionResult(type: str, status: str, detail: str)` with status `applied`, `noop`, `skipped`, `failed`.
  - `class Action(ABC)`: `execute(issue: Issue, event: PullRequestEvent, *, actor: User, connection: VcsConnection, templates: dict) -> ActionResult`. `SetState(state: str, fallback_group: str)`, `Comment(template: str)`. `build_action(spec: dict) -> Action`.
  - `SetState`: target = state named `state` in `issue.project` (case-insensitive), else lowest-`sequence` state of `fallback_group`; none → `skipped`; same as current → `noop`; else update `issue.state` and call `issue_activity.delay(type="issue.activity.updated", requested_data=json.dumps({"state_id": str(target.id)}), actor_id=str(actor.id), issue_id=..., project_id=..., current_instance=<json of plane.api.serializers.IssueSerializer(issue) before the change>, epoch=..., notification=True, origin=None)`.
  - `Comment`: renders the template with `html.escape`d values; `external_source="vcs"`, `external_id=f"{connection.id}:{event.repo_full_name}#{event.number}:{event.action}"`; existing comment with that external id → `noop`; else create `IssueComment(comment_html=..., actor=actor, ...)` and `issue_activity.delay(type="comment.activity.created", ...)` as the public comment endpoint does (`apps/api/plane/api/views/issue.py` ~line 1590).

- [ ] **Step 1: Write failing unit tests (rules):** `test_opened_selects_in_review`, `test_draft_opened_selects_nothing`, `test_merged_into_non_base_branch_selects_nothing`, `test_merged_requires_all_linked_merged`, `test_from_groups_blocks_cancelled`, `test_action_override_uses_given_action`.
- [ ] **Step 2: Write failing DB tests (actions):** `test_set_state_by_name`, `test_set_state_falls_back_to_group`, `test_set_state_skipped_when_no_state`, `test_set_state_noop_when_already_there` (no `issue_activity` call; patch `plane.integrations.vcs.actions.issue_activity`), `test_comment_escapes_title` (`<script>` rendered escaped), `test_comment_not_duplicated_for_same_event`.
- [ ] **Step 3: Run** → fail. **Step 4: Implement.** **Step 5: Run** → pass.
- [ ] **Step 6: Commit** `feat(vcs): rule engine and actions`.

### Task 6: Delivery service, Celery tasks and purge

**Files:**
- Create: `apps/api/plane/integrations/vcs/service.py`, `apps/api/plane/integrations/vcs/tasks.py`
- Modify: `apps/api/plane/settings/common.py` (add `"plane.integrations.vcs.tasks"` to `CELERY_IMPORTS`), `apps/api/plane/celery.py` (beat entry `purge_vcs_deliveries` daily at 03:15 UTC)
- Test: `apps/api/plane/tests/contract/app/vcs/test_service.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: `process_delivery(delivery: VcsDelivery) -> None` (sets `status`, `summary`, `processed_at`, `error`; raises only on transient errors); Celery `process_vcs_delivery(delivery_id: str)` with `autoretry_for=(OperationalError, InterfaceError)`, `retry_backoff=True`, `max_retries=3`, marking `failed` after the last retry; `purge_vcs_deliveries()` hard-deletes deliveries older than 30 days.
- Behaviour (spec "Event flow" 2-7): ping → `verified_at`, `ignored`; repo outside `connection.repo_filter` (fnmatch) → `ignored`; stale event (`updated_at` older than stored `provider_updated_at`) → PR not updated, no rules, summary `stale`; projects filtered by `only_enabled_projects` / disabled `VcsProjectSettings`; per work item `transaction.atomic()` and exceptions caught into the summary; effective settings = `merge_settings(connection.settings, project_settings.overrides)`; links newly created by this delivery on an open non-draft PR are evaluated with action `opened` (Review Focus 2); `all_linked_merged` computed over the work item's non-deleted links.
- Summary shape: `{"work_items": {"<issue_id>": {"identifier": "CODEA-40", "results": [{"type": "set_state", "status": "applied", "detail": "In Review"}]}}, "unresolved": ["UTF-8"]}`.

- [ ] **Step 1: Write failing tests** (deliveries built from the Task 3 fixtures, signed is not needed here): `test_opened_moves_to_in_review_and_comments`, `test_merged_moves_to_done`, `test_closed_unmerged_moves_back_to_in_progress`, `test_done_waits_for_all_linked_prs`, `test_merge_into_feature_branch_does_not_finish`, `test_cancelled_work_item_untouched_on_reopen`, `test_repo_outside_filter_ignored`, `test_stale_event_does_not_regress_pr_state`, `test_link_added_by_edit_on_open_pr_applies_opened_rules`, `test_unresolved_identifiers_are_ignored`, `test_project_override_changes_target_state`, `test_disabled_project_settings_skips_project`, `test_failure_on_one_work_item_does_not_stop_others`, `test_ping_marks_connection_verified`, `test_purge_removes_old_deliveries_only`.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(vcs): delivery processing and Celery tasks`.

### Task 7: Webhook endpoint

**Files:**
- Create: `apps/api/plane/integrations/vcs/webhook.py`
- Create: `apps/api/plane/app/urls/vcs.py`; modify `apps/api/plane/app/urls/__init__.py` to include it
- Test: `apps/api/plane/tests/contract/app/vcs/test_webhook.py`

**Interfaces:**
- Consumes: `registry` (Task 3), `process_vcs_delivery` (Task 6), models (Task 1).
- Produces: `VcsWebhookEndpoint` (DRF `APIView`, `authentication_classes = []`, `permission_classes = [AllowAny]`, scoped throttle `vcs_webhook` at `600/min` keyed by connection id) at `integrations/vcs/<str:provider>/<uuid:connection_id>/` (full path `/api/integrations/vcs/github/<id>/`), POST only. Reads `request.body` raw before parsing JSON.
- Responses: unknown/inactive connection or provider mismatch → 404 `{}`; bad signature → 401 `{}`; duplicate delivery id → 200 `{"status": "duplicate"}`; missing delivery id → 400; otherwise store delivery (`payload=provider.trim_payload(...)`), update `last_delivery_at`, `process_vcs_delivery.delay(str(delivery.id))` via `transaction.on_commit`, → 202 `{"status": "queued"}`.

- [ ] **Step 1: Write failing tests:** `test_valid_signature_queues_delivery` (patch `.delay`, assert called with the new delivery id), `test_invalid_signature_returns_401_and_stores_nothing`, `test_unknown_connection_returns_404`, `test_inactive_connection_returns_404`, `test_provider_mismatch_returns_404`, `test_duplicate_delivery_returns_200_without_requeue`, `test_endpoint_needs_no_session_or_api_key`, `test_end_to_end_eager_opened_moves_work_item` (eager Celery, signed fixture → work item in In Review).
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(vcs): webhook endpoint`.

### Task 8: Configuration API

**Files:**
- Create: `apps/api/plane/integrations/vcs/serializers.py`, `apps/api/plane/integrations/vcs/views.py`, `apps/api/plane/integrations/vcs/bot.py`
- Modify: `apps/api/plane/app/urls/vcs.py`
- Test: `apps/api/plane/tests/contract/app/vcs/test_config_api.py`

**Interfaces:**
- Consumes: Tasks 1-7.
- Produces: the endpoints of the spec table "Configuration API", under `workspaces/<str:slug>/` in `app/urls/vcs.py`, using `plane.app.views.base.BaseAPIView` and `plane.app.permissions` (`WorkSpaceAdminPermission` for connections and deliveries, `WorkspaceUserPermission` for providers, `ProjectAdminPermission` for project settings, `ProjectEntityPermission` plus restricted-guest check for the work item PR list).
- `bot.create_bot_user(workspace) -> User` creates the bot (`email=f"vcs-{uuid4().hex}@plane-bots.local"`, `is_bot=True`, `bot_type="VCS"`, `is_active=True`, unusable password) and an active `WorkspaceMember` with role 15.
- `VcsConnectionSerializer`: validates `provider` in registry, `settings` via `validate_settings` (default `DEFAULT_SETTINGS` when absent), `repo_filter` list of non-empty strings; `secret` write-never, returned only by create (`webhook_url` = `f"{request.build_absolute_uri('/')}api/integrations/vcs/{provider}/{id}/"`) and rotate-secret.
- `test-match` returns `[{"identifier": "CODEA-40", "work_item_id": "...", "name": "...", "matched_by": "branch"}]` using `extract_ids` + `resolve_work_items` with the connection's settings.
- Redeliver resets the delivery to `received`, clears `summary`/`error` and enqueues it.

- [ ] **Step 1: Write failing tests:** `test_admin_creates_connection_and_gets_secret_once` (GET afterwards has no `secret`), `test_member_cannot_manage_connections` (403), `test_invalid_settings_rejected_with_path` (400 contains `rules[0]`), `test_rotate_secret_changes_secret`, `test_deliveries_list_scoped_to_workspace` (other workspace's connection id → 404), `test_redeliver_requeues`, `test_test_match_returns_resolved_work_items`, `test_project_admin_sets_override`, `test_project_member_cannot_set_override`, `test_work_item_pull_requests_visible_to_viewer`, `test_restricted_guest_cannot_see_others_work_item_prs`, `test_providers_lists_github_with_schema`, `test_bot_user_created_with_connection`.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Run the full suite and lint** (Global Constraints commands) → all pass; `makemigrations db --check` → no changes.
- [ ] **Step 6: Commit** `feat(vcs): configuration API`.

### Task 9: End-to-end check (no code unless a defect appears)

- [ ] **Step 1:** Start the local stack (`docker-compose-local.yml`: plane-db, plane-redis, plane-mq, plane-minio, migrator, api, worker), seed a workspace, admin user and project `CODEA` with states Backlog, Todo, In Progress, In Review, Done, Cancelled and a work item CODEA-1.
- [ ] **Step 2:** Create a connection through the API; send the Task 3 fixtures (rewritten to reference CODEA-1) signed with the returned secret using `curl` in this order: opened, closed_merged into `develop`.
- [ ] **Step 3:** Expected: CODEA-1 goes to In Review with a comment, then to Done with a second comment; `deliveries/` lists both as `processed`; replaying the first delivery id returns 200 duplicate.
- [ ] **Step 4:** Record the result in the PR body; stop the stack with `down -v`.
