{
  description = "Upgrade a MariaDB or MySQL data directory one major version at a time";

  # Each server version comes from the newest nixpkgs release that has
  # it, so that its binaries are in the public cache.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    mariadb-10_3 = {
      flake = false;
      url = "github:NixOS/nixpkgs/nixos-20.03";
    };
    # 23.05's 10.4.31 needs OpenSSL 1.1, which nixpkgs marks insecure
    mariadb-10_4 = {
      flake = false;
      url = "github:NixOS/nixpkgs/nixos-22.11";
    };
    mariadb-10_5 = {
      flake = false;
      url = "github:NixOS/nixpkgs/nixos-24.11";
    };
    mariadb-10_6.follows = "nixpkgs";
    mariadb-10_11.follows = "nixpkgs";
    mariadb-11_4.follows = "nixpkgs";
    mariadb-11_8.follows = "nixpkgs";
    mysql-5_7 = {
      flake = false;
      url = "github:NixOS/nixpkgs/nixos-22.05";
    };
    mysql-8_0 = {
      flake = false;
      url = "github:NixOS/nixpkgs/nixos-25.11";
    };
    mysql-8_4.follows = "nixpkgs";
    tact = {
      inputs.nixpkgs.follows = "nixpkgs";
      url = "github:john-shaffer/tact";
    };
  };

  outputs =
    inputs@{ self, nixpkgs, ... }:
    let
      systems = [
        "aarch64-linux"
        "x86_64-linux"
      ];
      forSystems = f: nixpkgs.lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});
      servers =
        system:
        let
          from =
            input:
            import input {
              inherit system;
              config = { };
              overlays = [ ];
            };
        in
        {
          "mariadb-10.3" = (from inputs.mariadb-10_3).mariadb;
          "mariadb-10.4" = (from inputs.mariadb-10_4).mariadb_104;
          "mariadb-10.5" = (from inputs.mariadb-10_5).mariadb_105;
          "mariadb-10.6" = (from inputs.mariadb-10_6).mariadb_106;
          "mariadb-10.11" = (from inputs.mariadb-10_11).mariadb_1011;
          "mariadb-11.4" = (from inputs.mariadb-11_4).mariadb_114;
          "mariadb-11.8" = (from inputs.mariadb-11_8).mariadb_118;
          "mysql-5.7" = (from inputs.mysql-5_7).mysql57;
          "mysql-8.0" = (from inputs.mysql-8_0).mysql80;
          "mysql-8.4" = (from inputs.mysql-8_4).mysql84;
        };
      mysqlMultiUpgrade =
        pkgs:
        pkgs.runCommand "mysql-multi-upgrade"
          {
            meta.mainProgram = "mysql-multi-upgrade";
            nativeBuildInputs = [ pkgs.makeWrapper ];
          }
          ''
            mkdir -p $out/bin $out/libexec
            substitute ${./mysql_multi_upgrade.py} $out/libexec/mysql_multi_upgrade.py \
              --replace-fail '@flake@' 'path:${self}'
            makeWrapper ${pkgs.python3.interpreter} $out/bin/mysql-multi-upgrade \
              --add-flags $out/libexec/mysql_multi_upgrade.py
          '';
      fixtures =
        system: pkgs:
        let
          srv = servers system;
          # Only what builds fixtures, so that editing scenarios doesn't
          # rebuild them
          tests = nixpkgs.lib.fileset.toSource {
            root = ./tests;
            fileset = nixpkgs.lib.fileset.unions [
              ./tests/fingerprint.sql
              ./tests/lib.sh
              ./tests/make-fixture.sh
              ./tests/seed.sql
            ];
          };
          fixture =
            name: flavor: mode:
            pkgs.runCommand
              "mysql-multi-upgrade-fixture-${name}${nixpkgs.lib.optionalString (mode == "crash") "-crash"}"
              {
                nativeBuildInputs = with pkgs; [
                  hostname
                  procps
                ];
              }
              ''
                bash ${tests}/make-fixture.sh ${srv.${name}} ${flavor} $out ${mode}
              '';
          flavorOf = name: builtins.head (nixpkgs.lib.splitString "-" name);
          clean = map (name: {
            inherit name;
            path = fixture name (flavorOf name) "clean";
          }) (builtins.attrNames srv);
          crashed =
            map
              (name: {
                name = "${name}-crash";
                path = fixture name (flavorOf name) "crash";
              })
              [
                "mariadb-10.5"
                "mysql-5.7"
              ];
        in
        pkgs.linkFarm "mysql-multi-upgrade-fixtures" (clean ++ crashed);
      testsDir =
        pkgs:
        pkgs.runCommand "mysql-multi-upgrade-tests" { buildInputs = [ pkgs.bash ]; } ''
          cp -r ${./tests} $out
          chmod -R u+w $out
          patchShebangs $out
        '';
      # Always succeeds, so that Nix caches the result of a failing run
      scenarioReport =
        system: pkgs:
        let
          serversJson = pkgs.writeText "servers.json" (builtins.toJSON (servers system));
        in
        pkgs.runCommand "mysql-multi-upgrade-scenario-report"
          {
            nativeBuildInputs = with pkgs; [
              (mysqlMultiUpgrade pkgs)
              hostname
              inputs.tact.packages.${system}.default
              jq
              procps
            ];
          }
          ''
            mkdir -p $out
            export MYSQL_MULTI_UPGRADE_SERVERS=${serversJson}
            export FIXTURES=${fixtures system pkgs}
            export TESTS=${testsDir pkgs}
            export PATH=$TESTS/bin:$PATH
            jobs=''${NIX_BUILD_CORES:-1}
            [ "$jobs" -gt 0 ] || jobs=$(nproc)
            status=0
            tact --jobs "$jobs" --output $out/report.json $TESTS/scenarios > $out/log 2>&1 || status=$?
            echo "$status" > $out/exit-status
          '';
    in
    {
      checks = forSystems (
        system: pkgs: {
          scenarios = pkgs.runCommand "mysql-multi-upgrade-scenarios" { } ''
            if [ "$(cat ${scenarioReport system pkgs}/exit-status)" != 0 ]; then
              cat ${scenarioReport system pkgs}/log >&2
              exit 1
            fi
            touch $out
          '';
        }
      );

      devShells = forSystems (
        system: pkgs: {
          default = pkgs.mkShell {
            buildInputs = with pkgs; [
              inputs.tact.packages.${system}.default
              jq
              just
              python3
            ];
          };
        }
      );

      packages = forSystems (
        system: pkgs:
        servers system
        // {
          default = mysqlMultiUpgrade pkgs;
          fixtures = fixtures system pkgs;
          scenario-report = scenarioReport system pkgs;
        }
      );
    };
}
