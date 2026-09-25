#!/usr/bin/env bash
# Install a curated subset of the community Frappe skills into .claude/skills/ at a PINNED commit.
# Source: https://github.com/Impertio-Studio/Frappe_Claude_Skill_Package (61 skills, v3.2.0)
#
# Why pinned and not committed: skills are instructions injected into the agent's context, so an
# unreviewed upstream change is a supply-chain risk. Bump PIN deliberately after reading the diff.
# The repo's LICENSE.md is LGPL-3.0 text while README/package.json/SKILL.md say MIT; until that is
# clarified, keep the installed copies out of git (see .gitignore lines at the bottom).
set -euo pipefail

REPO="https://github.com/Impertio-Studio/Frappe_Claude_Skill_Package.git"
PIN="36cfa807518f48e4210fac2a5afc6adafad4c53e"   # 2026-04-01, v3.2.0
DEST="${1:-.claude/skills}"

SKILLS=(
  # syntax
  syntax/frappe-syntax-doctypes
  syntax/frappe-syntax-controllers
  syntax/frappe-syntax-hooks
  syntax/frappe-syntax-hooks-events
  syntax/frappe-syntax-whitelisted
  syntax/frappe-syntax-scheduler
  syntax/frappe-syntax-query-builder
  syntax/frappe-syntax-jinja
  syntax/frappe-syntax-clientscripts
  # core
  core/frappe-core-api
  core/frappe-core-database
  core/frappe-core-permissions
  core/frappe-core-logging
  core/frappe-core-translation
  # impl
  impl/frappe-impl-whitelisted
  # errors
  errors/frappe-errors-api
  errors/frappe-errors-database
  errors/frappe-errors-hooks
  errors/frappe-errors-controllers
  # testing, ops, agents
  testing/frappe-testing-unit
  testing/frappe-testing-cicd
  ops/frappe-ops-app-lifecycle
  agents/frappe-agent-debugger
)
# Deliberately NOT installed: frappe-core-cache (teaches frappe.lock(), which does not exist in
# v16), server-script skills, print/reports/workspace/workflow/ui skills, ops-cloud and the
# other ops skills, agent-validator/architect/interpreter/migrator.

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
git clone --quiet --filter=blob:none --no-checkout "$REPO" "$tmp"
git -C "$tmp" checkout --quiet "$PIN"

mkdir -p "$DEST"
for s in "${SKILLS[@]}"; do
  name="$(basename "$s")"
  [ -f "$tmp/skills/source/$s/SKILL.md" ] || { echo "missing upstream: $s" >&2; exit 1; }
  rm -rf "${DEST:?}/$name"
  cp -r "$tmp/skills/source/$s" "$DEST/$name"
done
echo "Installed ${#SKILLS[@]} skills at ${PIN:0:7} into $DEST"

# .gitignore (add once):
#   .claude/skills/frappe-*
#   CLAUDE.local.md
#   .claude/settings.local.json