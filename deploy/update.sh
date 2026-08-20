#!/usr/bin/env bash
# Manual updater: fetch the latest code, refresh dependencies and restart the bot.
#
#   sudo /opt/crafty-bot/deploy/update.sh
#
# Environment overrides:
#   REPO_DIR  checkout to update            (default: this script's repository)
#   BRANCH    branch to fast-forward to     (default: the checked-out branch)
#   REMOTE    git remote to fetch from      (default: origin)
#   SERVICE   systemd unit to restart       (default: crafty-bot)
#   VENV      virtualenv holding the deps   (default: $REPO_DIR/.venv)
#   FORCE     "1" to reinstall and restart even when already up to date
#
# The checkout is never modified in place by root: git runs as the user that owns
# the repository, exactly like the install instructions in the README.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REMOTE="${REMOTE:-origin}"
SERVICE="${SERVICE:-crafty-bot}"
VENV="${VENV:-$REPO_DIR/.venv}"
FORCE="${FORCE:-0}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 1; }

[[ -d "$REPO_DIR/.git" ]] || die "$REPO_DIR is not a git checkout."

REPO_OWNER="$(stat -c '%U' "$REPO_DIR")"
as_owner() {
  if [[ "$(id -un)" == "$REPO_OWNER" ]]; then
    "$@"
  else
    sudo -u "$REPO_OWNER" "$@"
  fi
}
as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

git_repo() { as_owner git -C "$REPO_DIR" "$@"; }

BRANCH="${BRANCH:-$(git_repo rev-parse --abbrev-ref HEAD)}"
[[ "$BRANCH" != "HEAD" ]] || die "The checkout is in a detached HEAD state; set BRANCH=<branch>."

if [[ -n "$(git_repo status --porcelain --untracked-files=no)" ]]; then
  die "The checkout has local modifications. Commit, stash or discard them first."
fi

info "Fetching $REMOTE/$BRANCH"
git_repo fetch --quiet "$REMOTE" "$BRANCH"

BEFORE="$(git_repo rev-parse HEAD)"
TARGET="$(git_repo rev-parse "$REMOTE/$BRANCH")"

if [[ "$BEFORE" == "$TARGET" && "$FORCE" != "1" ]]; then
  info "Already up to date ($(git_repo rev-parse --short HEAD)). Nothing to do."
  info "Run FORCE=1 $0 to reinstall dependencies and restart anyway."
  exit 0
fi

if [[ "$BEFORE" != "$TARGET" ]]; then
  info "Updating $(git_repo rev-parse --short HEAD) -> $(git_repo rev-parse --short "$REMOTE/$BRANCH")"
  git_repo checkout --quiet "$BRANCH"
  git_repo merge --ff-only --quiet "$REMOTE/$BRANCH"
  git_repo --no-pager log --oneline "$BEFORE..HEAD"
fi

DEPS_CHANGED=0
if [[ "$FORCE" == "1" ]]; then
  DEPS_CHANGED=1
elif ! git_repo diff --quiet "$BEFORE" HEAD -- requirements.txt; then
  DEPS_CHANGED=1
fi

if [[ "$DEPS_CHANGED" == "1" ]]; then
  if [[ -x "$VENV/bin/pip" ]]; then
    info "Installing dependencies into $VENV"
    as_owner "$VENV/bin/pip" install --quiet --upgrade -r "$REPO_DIR/requirements.txt"
  else
    warn "No virtualenv at $VENV; skipping dependency install."
  fi
else
  info "requirements.txt unchanged; skipping dependency install."
fi

if command -v systemctl >/dev/null && systemctl cat "$SERVICE.service" >/dev/null 2>&1; then
  info "Restarting $SERVICE"
  as_root systemctl restart "$SERVICE"
  sleep 3
  as_root systemctl status "$SERVICE" --no-pager --lines=15 || true
  if ! as_root systemctl is-active --quiet "$SERVICE"; then
    die "$SERVICE is not running. Check: journalctl -u $SERVICE -n 50"
  fi
else
  warn "No systemd unit called $SERVICE; restart the bot yourself."
  warn "Docker deployments: docker compose pull && docker compose up -d"
fi

info "Now running $(git_repo rev-parse --short HEAD) on $BRANCH."
