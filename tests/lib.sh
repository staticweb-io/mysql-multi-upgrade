# Starts and stops servers for the fixture and verify scripts.

# A short directory for sockets, since their paths have a length limit
run_dir() {
  local base="${TMPDIR:-/tmp}"
  [ "${#base}" -le 60 ] || base=/tmp
  mktemp -d "$base/dut.XXXXXX"
}

server_bin() {
  if [ -x "$1/bin/mariadbd" ]; then echo "$1/bin/mariadbd"; else echo "$1/bin/mysqld"; fi
}

client_bin() {
  if [ -x "$1/bin/mariadb" ]; then echo "$1/bin/mariadb"; else echo "$1/bin/mysql"; fi
}

# start_server SERVER DATADIR RUN_DIR [ARGS...]
start_server() {
  local server="$1" datadir="$2" run="$3"
  shift 3
  "$(server_bin "$server")" --no-defaults --basedir="$server" --datadir="$datadir" \
    --socket="$run/sock" --pid-file="$run/pid" --log-error="$run/server.err" \
    --skip-networking --innodb-use-native-aio=0 --tmpdir="$run" "$@" &
  SERVER_PID=$!
  for _ in $(seq 600); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "The server exited while starting:" >&2
      tail -30 "$run/server.err" >&2
      return 1
    fi
    if [ -S "$run/sock" ]; then return 0; fi
    sleep 0.2
  done
  echo "The server didn't start" >&2
  return 1
}

stop_server() {
  kill -TERM "$SERVER_PID"
  wait "$SERVER_PID"
}

# sql SERVER RUN_DIR USER PASSWORD [ARGS...]
sql() {
  local server="$1" run="$2" user="$3" password="$4"
  shift 4
  MYSQL_PWD="$password" "$(client_bin "$server")" --no-defaults --socket="$run/sock" \
    --user="$user" --batch --skip-column-names "$@"
}
