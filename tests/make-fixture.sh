#!/usr/bin/env bash
# make-fixture.sh SERVER FLAVOR OUT [crash]
# Creates OUT/data with the server at SERVER and seeds it, then shuts it
# down cleanly or, with "crash", kills it after more writes.
set -euo pipefail
here="$(dirname "$0")"
. "$here/lib.sh"
server="$1" flavor="$2" out="$3" mode="${4:-clean}"

mkdir -p "$out"
datadir="$out/data"
run="$(run_dir)"
if [ "$flavor" = mariadb ]; then
  install_db="$server/bin/mariadb-install-db"
  [ -x "$install_db" ] || install_db="$server/bin/mysql_install_db"
  "$install_db" --no-defaults --basedir="$server" --datadir="$datadir" \
    --auth-root-authentication-method=normal > "$run/install.log" 2>&1 \
    || { cat "$run/install.log" >&2; exit 1; }
else
  "$(server_bin "$server")" --no-defaults --basedir="$server" --datadir="$datadir" \
    --initialize-insecure --log-error="$run/install.log" \
    || { cat "$run/install.log" >&2; exit 1; }
fi

start_server "$server" "$datadir" "$run"
trap 'kill -KILL "$SERVER_PID" 2>/dev/null || true' EXIT
sql "$server" "$run" root "" < "$here/seed.sql"
sql "$server" "$run" root "" -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'root-password'" 2>/dev/null \
  || sql "$server" "$run" root "" -e "SET PASSWORD FOR 'root'@'localhost' = PASSWORD('root-password')"
if [ "$flavor" = mariadb ]; then
  sql "$server" "$run" root root-password -e \
    "CREATE TABLE app.aria_t (id INT PRIMARY KEY, v TEXT) ENGINE=Aria; INSERT INTO app.aria_t VALUES (1, 'aria')"
fi

if [ "$mode" = crash ]; then
  # Committed, so recovery must keep it
  sql "$server" "$run" root root-password -e \
    "INSERT INTO app.posts (title, body, created) VALUES ('Before the crash', 'x', '2022-01-01 00:00:00')"
  sql "$server" "$run" root root-password app < "$here/fingerprint.sql" > "$out/expected"
  kill -KILL "$SERVER_PID"
  wait "$SERVER_PID" || true
else
  sql "$server" "$run" root root-password app < "$here/fingerprint.sql" > "$out/expected"
  stop_server
fi
rm -rf "$run"
