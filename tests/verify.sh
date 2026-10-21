#!/usr/bin/env bash
# verify.sh SERVER DATADIR EXPECTED
# Checks that the server at SERVER runs DATADIR with its grants, that
# the data matches EXPECTED, and that the tables check out.
set -euo pipefail
here="$(dirname "$0")"
. "$here/lib.sh"
server="$1" datadir="$2" expected="$3"
run="$(run_dir)"
args=()
# Fixtures from older versions have mysql_native_password users
if "$(server_bin "$server")" --no-defaults --help --verbose 2>/dev/null | grep -q '^mysql-native-password '; then
  args+=(--mysql-native-password=ON)
fi
start_server "$server" "$datadir" "$run" "${args[@]}"
trap 'stop_server; rm -rf "$run"' EXIT

diff -u "$expected" <(sql "$server" "$run" app app-password app < "$here/fingerprint.sql")

# The trigger still fires. Rolled back, so verifying changes nothing.
counts="$(sql "$server" "$run" app app-password app -e "
  START TRANSACTION;
  SELECT n FROM stats;
  INSERT INTO posts (title, created) VALUES ('After', '2026-01-01 00:00:00');
  SELECT n FROM stats;
  ROLLBACK;")"
[ "$(echo "$counts" | tail -1)" -eq $(($(echo "$counts" | head -1) + 1)) ] \
  || { echo "The trigger didn't fire: $counts" >&2; exit 1; }

check="$(sql "$server" "$run" root root-password -e "CHECK TABLE app.posts, app.legacy, app.stats, mysql.user")"
# A MyISAM table that a crash left open gets a warning, but its status
# is still OK
if echo "$check" | grep -E $'\t(error|status)\t' | grep -v $'\tstatus\tOK$' | grep -q .; then
  echo "CHECK TABLE failed:" >&2
  echo "$check" >&2
  exit 1
fi

version="$(sql "$server" "$run" root root-password -e "SELECT VERSION()")"
echo "verified with $version"
