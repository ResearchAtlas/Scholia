#!/bin/bash
# Spike: build jgm/citeproc's executable as build/citeproc-wasm/citeproc.wasm with
# ghc-wasm-meta's GMP-free "native" flavour. Its prebuilt compiler runs on Linux x86_64.
set -euo pipefail
cd "$(dirname "$0")/../.."
META_REV=ded427a58615ce21597f945af25c778f9107e5f7  # ghc-wasm-meta, 2026-09-24
INDEX_STATE=2026-10-01T00:00:00Z  # Hackage as of this date
out=$PWD/build/citeproc-wasm
mkdir -p "$out"

# A toolchain restored from the CI cache is used only if this script installed it with
# the same revision and flavour; otherwise it is installed again (setup.sh replaces it).
toolchain="ghc-wasm-meta $META_REV FLAVOUR=native"
if [ "$(cat ~/.ghc-wasm/scholia-toolchain 2>/dev/null)" != "$toolchain" ]; then
  curl -fsSL "https://github.com/haskell-wasm/ghc-wasm-meta/archive/$META_REV.tar.gz" | tar -xz -C build
  FLAVOUR=native "build/ghc-wasm-meta-$META_REV/setup.sh"
  echo "$toolchain" > ~/.ghc-wasm/scholia-toolchain
fi
# shellcheck disable=SC1090
source ~/.ghc-wasm/env
wasm32-wasi-ghc --info > "$out/ghc-info.txt"
grep -E '"Project version"|"Target platform"' "$out/ghc-info.txt"
# The compiler's bignum backend must be the native one: a GMP build also exposes a GMP
# backend module.
wasm32-wasi-ghc-pkg dump | grep -oE '[A-Za-z.]*Bignum\.Backend[A-Za-z.]*|GHC\.Num\.Backend[A-Za-z.]*' \
  | sort -u | tee "$out/bignum-modules.txt"
if grep -qi gmp "$out/bignum-modules.txt" || ! grep -q 'Backend\.Native' "$out/bignum-modules.txt"; then
  echo "the compiler does not use the native bignum backend" >&2
  exit 1
fi

work=build/citeproc-src
rm -rf "$work" && mkdir -p "$work" && cd "$work"
wasm32-wasi-cabal update "hackage.haskell.org,$INDEX_STATE"
# The source packages built from local folders (Cabal checks only what it downloads itself).
# Each tarball must match its pinned SHA-256, which must also be the one in Hackage's
# signed index that `cabal update` verified, before it is unpacked.
index=~/.ghc-wasm/.cabal/packages/hackage.haskell.org/01-index.tar
while read -r package sha256; do
  curl -fsSL -o "$package.tar.gz" "https://hackage.haskell.org/package/$package/$package.tar.gz"
  echo "$sha256  $package.tar.gz" | sha256sum -c -
  signed=$(tar -xOf "$index" "${package%-*}/${package##*-}/package.json" \
    | jq -r '.signed.targets[].hashes.sha256' | sort -u)
  if [ "$signed" != "$sha256" ]; then
    echo "$package: the pinned SHA-256 differs from Hackage's index ($signed)" >&2
    exit 1
  fi
  tar -xzf "$package.tar.gz"
done <<'SOURCES'
citeproc-0.14 ced6c76b3d98d651e8de3dc7ed77f25199818bc48736e9225fe321b4784d4b9a
conduit-extra-1.3.8 491f3e8e9408f5d10ff8d02bf4d0edb11821e2537e7c22edbd6d64bf91388222
streaming-commons-0.2.3.1 33d16c0c6080795800d9f0e490203ea3e50bd5034e5d077a0ddc04d33ea4dc6e
xml-conduit-1.10.1.0 118ada3837b80c6327b11449bdab50d620043731be2b2494eadcd8e854bff83f
SOURCES

# WASI has no sockets: drop the network modules (and the network package) that
# citeproc never uses from conduit-extra and streaming-commons.
perl -i -ne 'print unless /^\s+Data\.Conduit\.Network(\.UDP|\.Unix)?\s*$/ || /^\s+,\s*network\s/' \
  conduit-extra-1.3.8/conduit-extra.cabal
perl -i -ne 'print unless /^\s+Data\.Streaming\.Network(\.Internal)?\s*$/ || /^\s+,\s*network\s/' \
  streaming-commons-0.2.3.1/streaming-commons.cabal
# A custom Setup.hs cannot run when cross-compiling; xml-conduit's only serves its doctests.
perl -i -ne 's/^build-type:\s+Custom/build-type: Simple/; print unless /^custom-setup/ || /^\s+setup-depends:/' \
  xml-conduit-1.10.1.0/xml-conduit.cabal

cat > cabal.project <<PROJECT
packages: citeproc-0.14 conduit-extra-1.3.8 streaming-commons-0.2.3.1 xml-conduit-1.10.1.0
index-state: $INDEX_STATE
tests: False
package citeproc
  flags: +executable
-- The native flavour's GHC (from GHC's main branch) no longer re-exports ghc-bignum's
-- modules from base, which some packages (hashable) still import through base.
package *
  ghc-options: -package ghc-bignum
package aeson
  flags: -ordered-keymap
package splitmix
  cc-options: -include sys/random.h
-- Upper bounds do not know GHC's main branch yet; without this the solver falls back to
-- ancient releases without bounds (aeson 0.9).
allow-newer: all
constraints: aeson >= 2.2
PROJECT

wasm32-wasi-cabal build --dry-run exe:citeproc | tee "$out/build-plan.txt"
wasm32-wasi-cabal build exe:citeproc
cp "$(wasm32-wasi-cabal list-bin exe:citeproc)" "$out/citeproc.wasm"
ls -l "$out/citeproc.wasm"
