#!/bin/bash
# Builds build/dist/AAB Research.app and build/AAB-Research.dmg on macOS (arm64).
#     tools/build_app.sh <verified llama.cpp archive from tools/fetch.py>
# Run it in the environment that builds the app (uv run). Every build is ad-hoc signed:
# no Developer ID, no notarization.
set -euo pipefail
cd "$(dirname "$0")/.."
archive=$1
app="build/dist/AAB Research.app"
# llama-server and the libraries it links, under the names it links them by
libraries=(
  libllama-server-impl.dylib libllama-common.0.dylib libmtmd.0.dylib libllama.0.dylib
  libggml.0.dylib libggml-base.0.dylib libggml-cpu.0.dylib libggml-blas.0.dylib
  libggml-metal.0.dylib libggml-rpc.0.dylib
)

# The helper: the release's own files, and its notices as the release prints them.
rm -rf build/llama.cpp && mkdir -p build/llama.cpp
tar -xzf "$archive" -C build/llama.cpp --strip-components 1
diff -u tools/notices/llama.cpp/LICENSES.txt <(build/llama.cpp/llama licenses) \
  || { echo "tools/notices/llama.cpp/LICENSES.txt differs from the release's notices" >&2; exit 1; }

python -m PyInstaller --noconfirm --clean --distpath build/dist --workpath build/work \
  tools/aab-research.spec

# The server goes in Contents/MacOS and its libraries in Contents/Frameworks/llama-cpp
# (codesign allows a dot in a Frameworks folder name only for frameworks).
helper_libs="$app/Contents/Frameworks/llama-cpp"
mkdir "$helper_libs"
for library in "${libraries[@]}"; do
  cp -L "build/llama.cpp/$library" "$helper_libs/"
done
cp build/llama.cpp/llama-server "$app/Contents/MacOS/"
install_name_tool -rpath @loader_path @loader_path/../Frameworks/llama-cpp \
  "$app/Contents/MacOS/llama-server"

# Ad-hoc signatures from the inside out, then the app's seal over everything.
codesign --force --sign - "$helper_libs"/*.dylib
codesign --force --sign - "$app/Contents/MacOS/llama-server"
codesign --force --sign - "$app"
codesign --verify --deep --strict --verbose=2 "$app"

# The disk image: the app and a link to Applications.
rm -rf build/dmg build/AAB-Research.dmg && mkdir build/dmg
ditto "$app" "build/dmg/AAB Research.app"
ln -s /Applications build/dmg/Applications
hdiutil create -volname "AAB Research" -srcfolder build/dmg -fs HFS+ -format UDZO \
  build/AAB-Research.dmg
