#!/bin/bash
# Spike: build jgm/citeproc's executable as build/citeproc-wasm/citeproc.wasm with
# ghc-wasm-meta's GMP-free "native" flavour. Its prebuilt compiler runs on Linux x86_64.
set -euo pipefail
cd "$(dirname "$0")/../.."
META_REV=ded427a58615ce21597f945af25c778f9107e5f7  # ghc-wasm-meta, 2026-09-24
INDEX_STATE=2026-10-01T00:00:00Z  # Hackage as of this date
out=$PWD/build/citeproc-wasm
mkdir -p "$out"

if [ ! -f ~/.ghc-wasm/env ]; then
  curl -fsSL "https://github.com/haskell-wasm/ghc-wasm-meta/archive/$META_REV.tar.gz" | tar -xz -C build
  FLAVOUR=native "build/ghc-wasm-meta-$META_REV/setup.sh"
fi
# shellcheck disable=SC1090
source ~/.ghc-wasm/env
wasm32-wasi-ghc --info > "$out/ghc-info.txt"
grep -E '"Project version"|"Target platform"' "$out/ghc-info.txt"
wasm32-wasi-ghc-pkg field ghc-bignum exposed-modules | tr ' ' '\n' | grep -i backend | tee "$out/bignum-modules.txt"

work=build/citeproc-src
rm -rf "$work" && mkdir -p "$work" && cd "$work"
wasm32-wasi-cabal update "hackage.haskell.org,$INDEX_STATE"
for package in citeproc-0.14 conduit-extra-1.3.8 streaming-commons-0.2.3.1 xml-conduit-1.10.1.0; do
  curl -fsSL "https://hackage.haskell.org/package/$package/$package.tar.gz" | tar -xz
done

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
package aeson
  flags: -ordered-keymap
package splitmix
  cc-options: -include sys/random.h
allow-newer: all:base, all:bytestring, all:containers, all:ghc-bignum, all:template-haskell, all:text, all:time, all:deepseq, all:ghc-prim
PROJECT

wasm32-wasi-cabal build exe:citeproc
cp "$(wasm32-wasi-cabal list-bin exe:citeproc)" "$out/citeproc.wasm"
ls -l "$out/citeproc.wasm"
