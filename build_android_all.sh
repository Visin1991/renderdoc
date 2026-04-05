#!/bin/bash
# Build both arm32 and arm64 Android APKs in one go
# Usage: bash build_android_all.sh [--clean]
#   --clean: Remove build directories and do a full rebuild
#   Without --clean: Incremental build (only recompile changed files)
# Run this script from the renderdoc root directory in MSYS2

# Parse arguments
DO_CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --clean) DO_CLEAN=1 ;;
    esac
done

echo "[DEBUG] Script started!"
echo "[DEBUG] Bash version: $BASH_VERSION"
echo "[DEBUG] Shell: $SHELL"
echo "[DEBUG] PWD: $(pwd)"

set -e

echo "[DEBUG] About to resolve SCRIPT_DIR..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "[DEBUG] SCRIPT_DIR=$SCRIPT_DIR"
cd "$SCRIPT_DIR"
echo "[DEBUG] Changed to dir: $(pwd)"

NPROC=$(nproc 2>/dev/null || echo 4)
echo "[DEBUG] NPROC=$NPROC"

# Detect generator
GENERATOR="Unix Makefiles"
echo "[DEBUG] uname: $(uname -a)"
if uname -a | grep -iq msys; then
    GENERATOR="MSYS Makefiles"
fi
echo "[DEBUG] GENERATOR=$GENERATOR"


export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"
export ANDROID_SDK="/c/Users/wisinzhu/AppData/Local/Android/Sdk"
export ANDROID_NDK="/c/Users/wisinzhu/AppData/Local/Android/Sdk/ndk/26.1.10909125"
export PATH="/c/Program Files/Android/Android Studio/jbr/bin:$PATH" 
export PATH="/c/Program Files/CMake/bin:$PATH"
export PATH="/c/Program Files/Git/cmd:$PATH"

echo "[DEBUG] JAVA_HOME=$JAVA_HOME"
echo "[DEBUG] ANDROID_SDK=$ANDROID_SDK"
echo "[DEBUG] ANDROID_NDK=$ANDROID_NDK"
echo "[DEBUG] java: $(which java 2>/dev/null || echo 'NOT FOUND')"
echo "[DEBUG] cmake: $(which cmake 2>/dev/null || echo 'NOT FOUND')"
echo "[DEBUG] git: $(which git 2>/dev/null || echo 'NOT FOUND')"

# Get the current git commit hash to ensure version consistency
# between Windows build and Android build
echo "[DEBUG] About to get git hash..."
GIT_HASH=$(git rev-parse HEAD 2>/dev/null || true)
echo "[DEBUG] GIT_HASH=$GIT_HASH"
if [ -z "$GIT_HASH" ]; then
    echo "WARNING: Could not get git commit hash. Version mismatch may occur!"
else
    echo "Git commit hash: $GIT_HASH"
    echo "This will be embedded in APKs to match the Windows build."
fi

echo ""
echo "============================================"
echo "  Building Android arm32 (armeabi-v7a)"
echo "============================================"

if [ "$DO_CLEAN" -eq 1 ]; then
    echo "[DEBUG] Clean build: Removing old build-android-arm32..."
    rm -rf build-android-arm32
else
    echo "[DEBUG] Incremental build: Keeping existing build-android-arm32"
fi
mkdir -p build-android-arm32
echo "[DEBUG] pushd build-android-arm32"
pushd build-android-arm32

echo "[DEBUG] Running cmake for arm32..."
cmake -G "${GENERATOR}"    -DBUILD_ANDROID=On \
    -DANDROID_ABI=armeabi-v7a \
    -DANDROID_NATIVE_API_LEVEL=23 \
    -DCMAKE_BUILD_TYPE=Release \
    ${GIT_HASH:+-DBUILD_VERSION_HASH=$GIT_HASH} \
    ..

echo "[DEBUG] Running make for arm32..."
make -j${NPROC}
echo "[DEBUG] arm32 make finished."

if ! ls bin/*.apk 2>/dev/null; then
    echo "ERROR: arm32 APK build failed!"
    exit 1
fi

echo "arm32 APK built successfully."
echo "[DEBUG] popd from arm32"
popd

echo ""
echo "============================================"
echo "  Building Android arm64 (arm64-v8a)"
echo "============================================"

if [ "$DO_CLEAN" -eq 1 ]; then
    echo "[DEBUG] Clean build: Removing old build-android-arm64..."
    rm -rf build-android-arm64
else
    echo "[DEBUG] Incremental build: Keeping existing build-android-arm64"
fi
mkdir -p build-android-arm64
echo "[DEBUG] pushd build-android-arm64"
pushd build-android-arm64

echo "[DEBUG] Running cmake for arm64..."
cmake -G "${GENERATOR}"    -DBUILD_ANDROID=On \
    -DANDROID_ABI=arm64-v8a \
    -DANDROID_NATIVE_API_LEVEL=23 \
    -DCMAKE_BUILD_TYPE=Release \
    ${GIT_HASH:+-DBUILD_VERSION_HASH=$GIT_HASH} \
    ..

echo "[DEBUG] Running make for arm64..."
make -j${NPROC}
echo "[DEBUG] arm64 make finished."

if ! ls bin/*.apk 2>/dev/null; then
    echo "ERROR: arm64 APK build failed!"
    exit 1
fi

echo "arm64 APK built successfully."
echo "[DEBUG] popd from arm64"
popd

echo ""
echo "============================================"
echo "  Collecting APKs into build-android/bin/"
echo "============================================"

# Create the unified output directory that RenderDoc searches
mkdir -p build-android/bin

# Copy both APKs into the unified directory
cp build-android-arm32/bin/*.apk build-android/bin/
cp build-android-arm64/bin/*.apk build-android/bin/

echo ""
echo "Done! APKs are in build-android/bin/:"
ls -la build-android/bin/*.apk

echo ""
echo "============================================"
echo "  Build complete!"
echo "============================================"
