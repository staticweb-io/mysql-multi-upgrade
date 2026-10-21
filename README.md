# mysql-multi-upgrade

Upgrades a MariaDB or MySQL data directory in place, one major
version at a time, until it reaches the version you want.

```console
$ nix run github:john-shaffer/mysql-multi-upgrade -- upgrade --to 11.8 /var/lib/mysql
/var/lib/mysql is mariadb 10.3.22
Steps: mariadb-10.3 -> mariadb-10.4 -> mariadb-10.5 -> mariadb-10.6 -> mariadb-10.11 -> mariadb-11.4 -> mariadb-11.8
...
Upgraded /var/lib/mysql from mariadb 10.3.22 to mariadb 11.8.8
```

Stop the server and back up the data directory first.
Each step changes it in a way that older versions can't read.

## Supported versions

| Flavor  | Versions                                  |
| ------- | ----------------------------------------- |
| MariaDB | 10.3, 10.4, 10.5, 10.6, 10.11, 11.4, 11.8 |
| MySQL   | 5.7, 8.0, 8.4                             |

Each version comes from the newest nixpkgs release that has it,
pinned as its own input in `flake.nix`, so its binaries come from
the public Nix cache.
Every step is between versions with a documented upgrade path,
so the MariaDB short-term releases (10.7 to 10.10, 11.0 to 11.3,
11.5 to 11.7) aren't steps.
A data directory from one of them starts at the next version in
the chain.

MySQL data directories can't become MariaDB ones or the reverse.

## How it works

`mysql-multi-upgrade detect DATADIR` finds the newest version that has
written to the data directory, from:

- `mariadb_upgrade_info`, `mysql_upgrade_info` and `mysql_upgrade_history`
- The creator in the InnoDB redo log headers
- The version in each table's `.frm` file

`mysql-multi-upgrade plan DATADIR [--to VERSION]` prints the versions
that an upgrade would run.

`mysql-multi-upgrade upgrade DATADIR [--to VERSION]` builds each of
those servers with `nix build`, then runs each in turn:

1. Start the server with `--skip-grant-tables`, so no password is
   needed, and `--skip-networking`.
   The first server is the data directory's own version, so that it
   recovers from a crash and finishes any upgrade that was never run.
2. Run `mariadb-upgrade` or `mysql_upgrade`.
   MySQL 8.0 and later upgrade themselves as they start.
3. Shut down with `--innodb-fast-shutdown=0`, so that the next
   version has nothing to recover.

It stops at the first failure and prints the end of that server's
log.
The data directory is then at the last version that succeeded, and
running `upgrade` again continues from there.

After an upgrade to MySQL 8.4, it warns about users that use
`mysql_native_password`, which 8.4 disables by default.

### Options

- `--defaults-file FILE`: an option file for each server, instead of
  `--no-defaults`. Use it for settings that a data directory depends
  on, such as `innodb_page_size` or `lower_case_table_names`.
- `--mysqld-arg ARG`: an extra server argument. Repeatable.
- `--log-dir DIR`: where to keep the server logs.
- `--server NAME=PATH`: use the server at `PATH`, such as
  `mariadb-10.4=/nix/store/...-mariadb-server-10.4.27`.
  `MYSQL_MULTI_UPGRADE_SERVERS` can name a JSON file with the same map.
- `--flake REF`: build servers from another flake.

Run it as the data directory's owner, or as root, in which case each
server runs as the owner.

## Development

`just test` runs the [tact](https://github.com/john-shaffer/tact)
scenarios in `tests/scenarios` against data directories that each
supported version creates and seeds in a Nix build.
A scenario upgrades one with real servers, then checks its data,
logins, views, procedures and triggers with the newest server.
