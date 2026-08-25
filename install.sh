#!/usr/bin/env sh
set -eu

repository='https://github.com/ThisisPeggy/-Tale-Hermes-Connector'
plugin_name='hermes-browser'
hermes_home="${HERMES_HOME:-$HOME/.hermes}"
plugin_dir="$hermes_home/plugins/$plugin_name"
gateway_stopped=0
revision="${HERMES_BROWSER_CONNECTOR_COMMIT:-origin/main}"

case "$revision" in
  origin/main) ;;
  *[!0-9a-fA-F]*|'') echo 'HERMES_BROWSER_CONNECTOR_COMMIT must be a 40-character Git commit.' >&2; exit 1 ;;
  *) [ "${#revision}" -eq 40 ] || { echo 'HERMES_BROWSER_CONNECTOR_COMMIT must be a 40-character Git commit.' >&2; exit 1; } ;;
esac

restart_gateway() {
  if [ "$gateway_stopped" -eq 1 ]; then
    hermes gateway restart || true
  fi
}
trap restart_gateway EXIT INT TERM

command -v hermes >/dev/null 2>&1 || { echo 'Hermes is not installed or is not on PATH.' >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo 'Git is not installed or is not on PATH.' >&2; exit 1; }

hermes gateway stop >/dev/null 2>&1 || true
gateway_stopped=1

if [ -d "$plugin_dir" ] && git -C "$plugin_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo 'Updating Tale Hermes Connector...'
else
  if [ -e "$plugin_dir" ]; then
    backup_root="$hermes_home/plugin-backups"
    backup_path="$backup_root/hermes-browser-$(date '+%Y%m%d-%H%M%S')"
    echo 'Repairing an incomplete Connector installation...'
    mkdir -p "$backup_root"
    mv "$plugin_dir" "$backup_path"
    echo "Moved the incomplete Connector to $backup_path"
  fi
  echo 'Installing Tale Hermes Connector...'
  hermes plugins install "$repository" --enable
fi

git -C "$plugin_dir" remote set-url origin "$repository"
if [ "$revision" = 'origin/main' ]; then
  git -C "$plugin_dir" fetch --prune origin
else
  git -C "$plugin_dir" fetch --no-tags origin "$revision"
fi
git -C "$plugin_dir" checkout --force "$revision"
hermes plugins enable "$plugin_name" --no-allow-tool-override

if command -v python3 >/dev/null 2>&1; then
  python3 "$plugin_dir/connect.py"
else
  python "$plugin_dir/connect.py"
fi

echo 'Tale Hermes Connector is ready.'
