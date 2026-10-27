{
  description = "Soloify: Spotify Soloist for Q2 Pod devices, through a home bridge and a card relay";

  inputs = {
    # The same revision as q2-pod's lock, so the two shells share the store.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          # Soloist is closed source, built for an FHS glibc system: run it through Nix's own loader
          # with the libraries it links (libatomic) and the ones it dlopens (libpulse, libpipewire,
          # ffmpeg's). It reads PipeWire's client.conf only from standard paths, which NixOS lacks,
          # so without one it is pointed at the package's. The binary itself is never in the store
          # (Spotify forbids redistributing it): SOLOIST_BIN, else data/soloist/soloist.
          soloist = pkgs.writeShellApplication {
            name = "soloist";
            runtimeInputs = [ ];
            text = ''
              bin=''${SOLOIST_BIN:-$PWD/data/soloist/soloist}
              if [ ! -x "$bin" ]; then
                echo "soloist: no binary at $bin (run tools/soloist-fetch, or set SOLOIST_BIN)" >&2
                exit 127
              fi
              if [ -z "''${PIPEWIRE_CONFIG_DIR:-}" ] && [ ! -e /etc/pipewire/client.conf ]; then
                export PIPEWIRE_CONFIG_DIR=${pkgs.pipewire}/share/pipewire
              fi
              exec ${pkgs.stdenv.cc.bintools.dynamicLinker} \
                --library-path ${
                  pkgs.lib.makeLibraryPath [
                    pkgs.stdenv.cc.cc.lib
                    pkgs.libpulseaudio
                    pkgs.pipewire
                    pkgs.ffmpeg
                  ]
                } "$bin" "$@"
            '';
          };
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          linux = pkgs.stdenv.hostPlatform.isLinux;
          python = pkgs.python3.withPackages (
            ps: with ps; [
              aiohttp
              cryptography
              pytest
              pytest-asyncio
            ]
          );
        in
        {
          default = pkgs.mkShell {
            packages =
              with pkgs;
              [
                # bridge
                python
                ruff
                ffmpeg
                openssl
                curl
                # relay: q2-pod's helper recipe (clang + ld.lld for o32 nan2008 fp64 MIPS)
                llvmPackages.clang-unwrapped
                lld
                llvm
                # container image and compose files
                hadolint
                docker-compose
              ]
              ++ pkgs.lib.optionals linux [
                self.packages.${system}.soloist
                pulseaudio # pactl, parec: the null sink and its monitor
                pipewire # pw-cli, pw-dump
                qemu-user # the relay's host tests, under qemu-mipsel with the stock rootfs
                squashfs-tools # unsquashfs: the stock rootfs's libraries, for the relay's link
                nftables # tools/soloify-stall
              ];

            # the relay's host build (tests): OpenSSL's headers and libraries
            buildInputs = [ pkgs.openssl ];

            hardeningDisable = [ "all" ];

            shellHook = ''
              export PYTHONPATH="$PWD/bridge''${PYTHONPATH:+:$PYTHONPATH}"
              # nixpkgs' ffmpeg loads a mismatched libasound with an inherited LD_LIBRARY_PATH
              unset LD_LIBRARY_PATH
            '';
          };
        }
      );

      formatter = forAllSystems (system: (import nixpkgs { inherit system; }).nixfmt);
    };
}
