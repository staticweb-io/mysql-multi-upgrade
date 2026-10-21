alias fmt := format
alias t := test
alias u := update

[private]
list:
    @# First command in the file is invoked by default
    @just --list

check: test
    nix flake check

# Format source code and data
format:
    sand format

# Upgrade a data dir with servers built from this checkout
run *args:
    python3 mysql_multi_upgrade.py {{ args }}

# Run all scenarios
test:
    #!/usr/bin/env bash
    set -euo pipefail
    out=$(nix build .#scenario-report --print-out-paths)
    cat "$out/log"
    exit "$(cat "$out/exit-status")"

# Run scenarios outside the sandbox against this checkout's script
test-local *paths="tests/scenarios":
    #!/usr/bin/env bash
    set -euo pipefail
    bin="$(mktemp -d)"
    trap 'rm -rf "$bin"' EXIT
    printf '#!/bin/sh\nexec python3 %s "$@"\n' "$PWD/mysql_multi_upgrade.py" > "$bin/mysql-multi-upgrade"
    chmod +x "$bin/mysql-multi-upgrade"
    nix eval --json ".#packages.$(nix eval --impure --raw --expr builtins.currentSystem)" --apply \
      'p: builtins.mapAttrs (n: v: "${v}") (removeAttrs p ["default" "fixtures" "scenario-report"])' \
      > "$bin/servers.json"
    export MYSQL_MULTI_UPGRADE_SERVERS="$bin/servers.json"
    export FIXTURES="$(nix build --no-link --print-out-paths .#fixtures)"
    export TESTS="$PWD/tests"
    export PATH="$bin:$TESTS/bin:$PATH"
    tact --jobs "$(nproc)" {{ paths }}

# Upgrade a fixture with servers from `nix build`, outside the sandbox
smoke fixture="mariadb-10.3":
    #!/usr/bin/env bash
    set -euo pipefail
    dir="$(mktemp -d)"
    trap 'rm -rf "$dir"' EXIT
    cp -r "$(nix build .#fixtures --print-out-paths)/{{ fixture }}/data" "$dir/data"
    chmod -R u+w "$dir/data"
    python3 mysql_multi_upgrade.py upgrade "$dir/data"

# Update dependencies
update:
    nix flake update
