#!/usr/bin/env bash
# Deploy the jira-search Claude agent skill.
#
# This script does ONE job: put the skill where Claude Code will find it. It
# never touches credentials. Tokens are per-user and belong to a separate
# command, `jira-login`, which ships inside the skill so the people who
# need it have it even if they never see this repo.
#
#   maintainer, once   clone this repo somewhere group-readable
#   maintainer/user    ./install.sh              deploy the skill
#   each user, once    jira-login                install their own token
#
# The code is shared; the credential never is.
set -euo pipefail

SKILL_NAME="jira-search"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd -P)"
# claude/ and opencode/ hold identical copies of the skill; either works as the
# source. The claude/ side is the one a local Claude Code user deploys.
SRC="$REPO_ROOT/claude/skills/$SKILL_NAME"
DEST_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
MODE=""                  # empty = decide from the destination; see below
FORCE=0
UNINSTALL=0

usage() {
  cat <<EOF
usage: install.sh [options]

Deploys the skill. Does not touch tokens — see 'jira-login' for that.

  --copy       copy the skill (default for a destination outside your home)
  --symlink    symlink the skill (default for a destination under your home)
  --dir DIR    deploy into DIR instead of \$CLAUDE_SKILLS_DIR or ~/.claude/skills
  --force      replace whatever is already at the destination
  --uninstall  remove the deployed skill (leaves your token alone)

Symlinking a shared multi-user tree back into a personal clone gives every other
user a link they cannot read, so the default flips to copying when the
destination is not under your home directory. Override either way.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --copy)      MODE="copy"; shift ;;
    --symlink)   MODE="symlink"; shift ;;
    --dir)       DEST_DIR="${2:?--dir needs a directory}"; shift 2 ;;
    --force)     FORCE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           echo "install.sh: unknown option '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

DEST="$DEST_DIR/$SKILL_NAME"

# --- is what is at DEST ours? -----------------------------------------------
# "Ours" is either a symlink pointing anywhere inside this clone — including a
# dangling one, since moving the skill within the repo breaks every previous
# deployment — or a real directory whose SKILL.md declares this skill's name.
# Acting on our own deployment must not need --force; touching a stranger's
# must. Both install and uninstall ask this same question, so it is asked in one
# place: a copy is the DEFAULT deployment for any destination outside your home,
# which is exactly the shared install the README documents, and an uninstall
# that only recognised symlinks could not remove one.
is_ours() {
  if [ -L "$1" ]; then
    case "$(readlink "$1")" in "$REPO_ROOT"/*) return 0 ;; esac
    return 1
  fi
  [ -d "$1" ] && [ -f "$1/SKILL.md" ] &&
    grep -qE "^name:[[:space:]]*$SKILL_NAME[[:space:]]*$" "$1/SKILL.md"
}

# --- uninstall --------------------------------------------------------------
# Before the symlink-or-copy decision below, deliberately: that block prints a
# paragraph about how the deployment will be made, and on the way out nothing is
# being deployed. Nothing after this point runs on the uninstall path.
if [ "$UNINSTALL" -eq 1 ]; then
  if [ ! -e "$DEST" ] && [ ! -L "$DEST" ]; then
    echo "not deployed: $DEST"
  elif is_ours "$DEST" || [ "$FORCE" -eq 1 ]; then
    rm -rf "$DEST"
    echo "removed $DEST"
  else
    echo "install.sh: $DEST is not ours — it is neither a link into" >&2
    echo "            $REPO_ROOT" >&2
    echo "            nor a directory whose SKILL.md says 'name: $SKILL_NAME'." >&2
    echo "            re-run with --force if you are sure." >&2
    exit 1
  fi
  echo "note: your token file was left in place. Remove it by hand if you"
  echo "      meant to revoke access on this machine."
  exit 0
fi

# --- symlink or copy? -------------------------------------------------------
# A symlink points back into this clone. That is what you want for your own
# ~/.claude/skills, and wrong for a shared multi-user tree: every other user
# then follows a link into a directory they usually cannot read. Default on
# where the destination lives, not on what is convenient here.
if [ -z "$MODE" ]; then
  home="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)"
  home="${home:-$HOME}"
  # Check the path as written AND as resolved. A personal ~/.claude is often a
  # symlink into a project directory; resolving first would call that "shared"
  # and silently downgrade a personal deployment to a copy. Asking for a path
  # under your own home is taken at face value.
  dest_abs="$(realpath -m "$DEST_DIR" 2>/dev/null || printf '%s' "$DEST_DIR")"
  MODE="copy"
  case "$DEST_DIR/" in "$home"/*) MODE="symlink" ;; esac
  case "$dest_abs/" in "$home"/*) MODE="symlink" ;; esac
  if [ "$MODE" = "copy" ]; then
    echo "note: $DEST_DIR is outside your home directory, so deploying a copy."
    echo "      A symlink there would point into"
    echo "      $REPO_ROOT,"
    echo "      which other users may not be able to read."
    echo "      Pass --symlink to override, and re-run after a git pull to"
    echo "      update the copy."
  fi
fi

# --- sanity: is the source actually here and intact? ------------------------
for f in "$SRC/SKILL.md" "$SRC/scripts/jqlsearch.py" "$SRC/scripts/jira-login"; do
  [ -f "$f" ] || { echo "install.sh: missing $f — run this from the clone" >&2; exit 1; }
done
chmod +x "$SRC/scripts/jqlsearch.py" "$SRC/scripts/jira-login" 2>/dev/null || true

mkdir -p "$DEST_DIR"

# --- refuse to clobber someone else's skill ---------------------------------
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  if [ -L "$DEST" ] && [ "$(readlink "$DEST")" = "$SRC" ]; then
    echo "already deployed: $DEST -> $SRC"
  elif is_ours "$DEST"; then
    [ -e "$DEST" ] || echo "note: $DEST was a dangling link into this clone"
    rm -rf "$DEST"
    echo "replacing previous deployment at $DEST"
  elif [ "$FORCE" -ne 1 ]; then
    echo "install.sh: $DEST already exists and is not ours." >&2
    echo "            re-run with --force to replace it." >&2
    exit 1
  else
    rm -rf "$DEST"
  fi
fi

if [ ! -e "$DEST" ] && [ ! -L "$DEST" ]; then
  case "$MODE" in
    symlink) ln -s "$SRC" "$DEST"; echo "deployed $DEST -> $SRC" ;;
    copy)
      cp -R "$SRC" "$DEST"
      # A copy into a shared tree is useless if the group cannot read it, which
      # is what an inherited umask of 027 produces. Group ownership comes from
      # the setgid parent; only the mode needs fixing.
      chmod -R g+rX "$DEST" 2>/dev/null || true
      echo "deployed $SRC -> $DEST (copy)"
      ;;
  esac
fi

# --- environment notes ------------------------------------------------------
# uv is the supported path: the skill's PEP 723 metadata pins python>=3.9 and uv
# provisions exactly that. A bare python3 is often the system one — 3.6 on these
# login nodes — which cannot even parse the script.
if ! command -v uv >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1 &&
     python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    :
  else
    echo
    echo "warning: no usable interpreter."
    echo "         uv is not installed, and python3 is $(python3 -V 2>&1 || echo absent),"
    echo "         but this skill needs python >= 3.9. The skill is deployed but"
    echo "         will not run until you install uv (https://docs.astral.sh/uv/)."
  fi
elif [ -z "${UV_CACHE_DIR:-}" ]; then
  echo
  echo "note: UV_CACHE_DIR is unset, so uv caches under ~/.cache/uv."
  echo "      On quota'd home directories set UV_CACHE_DIR=/tmp/uv-cache-\$USER."
fi

cat <<EOF

Deployed. Each user now installs their own token — once, on their own account:

  $DEST/scripts/jira-login

Search results are filtered by whoever's token is in play, so this step cannot
be done for them.
EOF
