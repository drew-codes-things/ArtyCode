#!/usr/bin/env bash
set -euo pipefail

MIN_PYTHON_MINOR=11
APP_HOME="${ARTY_CLI_HOME:-$HOME/.local/share/arty-cli}"
BIN_DIR="${ARTY_CLI_BIN:-$HOME/.local/bin}"
SOURCE="${ARTY_CLI_SOURCE:-}"
MARKER=".arty-cli-install"

say() { printf '%s\n' "$*"; }
fail() { printf 'arty-cli install: %s\n' "$*" >&2; exit 1; }

points_into_app_home() {
  local link="$1" target
  [ -L "$link" ] || return 1
  target="$(readlink "$link")"
  case "$target" in
    "$APP_HOME"/*) return 0 ;;
    *) return 1 ;;
  esac
}

is_arty_install() {
  [ -d "$APP_HOME" ] && [ ! -L "$APP_HOME" ] || return 1
  [ -f "$APP_HOME/$MARKER" ] && return 0
  [ -e "$APP_HOME/venv/bin/arty-cli" ] || return 1
  [ "$(ls -A "$APP_HOME")" = "venv" ]
}

uninstall() {
  local name
  if [ -e "$APP_HOME" ] && ! is_arty_install; then
    fail "$APP_HOME is not an arty-cli install (no $MARKER, and it holds more than arty-cli's venv), so nothing was removed."
  fi
  for name in arty-cli arty; do
    if points_into_app_home "$BIN_DIR/$name"; then
      rm -f "$BIN_DIR/$name"
    elif [ -e "$BIN_DIR/$name" ] || [ -L "$BIN_DIR/$name" ]; then
      say "Left $BIN_DIR/$name alone: it isn't part of this arty-cli install."
    fi
  done
  if [ -d "$APP_HOME" ]; then
    rm -rf "$APP_HOME/venv"
    rm -f "$APP_HOME/$MARKER"
    rmdir "$APP_HOME" 2>/dev/null || say "Left $APP_HOME in place: it holds files arty-cli didn't put there."
  fi
  say "Removed arty-cli. Your settings and conversations in ~/.config/arty-cli were kept."
}

link_command() {
  local name="$1" link="$BIN_DIR/$1"
  if [ -e "$link" ] || [ -L "$link" ]; then
    if ! points_into_app_home "$link"; then
      say "Warning: $link already exists and isn't from arty-cli, so it was left alone. Use arty-cli instead."
      return 1
    fi
  fi
  ln -sfn "$APP_HOME/venv/bin/$name" "$link"
}

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PYTHON_MINOR) else 1)" 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

script_folder() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [ -n "$here" ] && [ -f "$here/pyproject.toml" ] && [ -d "$here/arty_cli" ]; then
    printf '%s' "$here"
  fi
}

chosen_source() {
  if [ -n "$SOURCE" ]; then
    printf '%s' "$SOURCE"
    return
  fi
  local here
  here="$(script_folder)"
  [ -n "$here" ] || fail "no source to install from. Run install.sh from an arty-cli folder, or set ARTY_CLI_SOURCE to a folder, a .tar.gz/.zip URL or a git URL."
  printf '%s' "$here"
}

package_spec() {
  local source="$1"
  case "$source" in
    http://*|git+http://*) fail "$source uses plain http; use an https address so the download can't be changed on the way." ;;
    git+*) printf '%s' "arty-cli @ $source" ;;
    https://*)
      case "$source" in
        *.git) printf '%s' "arty-cli @ git+$source" ;;
        *) printf '%s' "arty-cli @ $source" ;;
      esac ;;
    *)
      [ -f "$source/pyproject.toml" ] || fail "$source is not an arty-cli folder (no pyproject.toml)."
      printf '%s' "$(cd "$source" && pwd)" ;;
  esac
}

install() {
  local python source spec
  python="$(find_python)" || fail "Python 3.$MIN_PYTHON_MINOR or newer is needed. On Debian: sudo apt install python3 python3-venv"
  "$python" -c "import venv, ensurepip" 2>/dev/null || fail "Python's venv module is missing. On Debian: sudo apt install python3-venv"
  source="$(chosen_source)"
  spec="$(package_spec "$source")"
  say "Installing arty-cli from $source into $APP_HOME"
  if [ -e "$APP_HOME" ] && [ -n "$(ls -A "$APP_HOME" 2>/dev/null)" ] && ! is_arty_install; then
    fail "$APP_HOME already holds other files; choose an empty folder with ARTY_CLI_HOME."
  fi
  mkdir -p "$APP_HOME" "$BIN_DIR"
  : > "$APP_HOME/$MARKER"
  [ -x "$APP_HOME/venv/bin/python" ] || "$python" -m venv "$APP_HOME/venv"
  "$APP_HOME/venv/bin/python" -m pip install --quiet --upgrade pip
  "$APP_HOME/venv/bin/python" -m pip install --quiet --upgrade "$spec"
  link_command arty-cli || fail "$BIN_DIR/arty-cli belongs to something else; move it and run install.sh again."
  local start_with=arty
  link_command arty || start_with=arty-cli
  say "Installed $("$BIN_DIR/arty-cli" --version)."
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "Add $BIN_DIR to your PATH, e.g.: echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc" ;;
  esac
  say "Type $start_with to start (the first run asks how to log in; $start_with login changes it later)."
}

case "${1:-}" in
  --uninstall) uninstall ;;
  "") install ;;
  *) fail "unknown option $1 (use --uninstall to remove)" ;;
esac
