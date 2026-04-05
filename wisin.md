
# Android Build

## MSYS2
安装MSYS2，然后运行 MSYS MINGW 64
pacman -S make 

## 安装完整的开发工具链
pacman -S base-devel mingw-w64-x86_64-toolchain
pacman -S mingw-w64-x86_64-cmake 

验证C++和编译器版本
g++ --version
c++ --version

## 直接运行 build_android_all.sh
直接运行 build_android_all.sh脚本，各种环境设置导出都已经处理好了。
主要JAVA AndroidSDK NDK 这些都需要在Windows环境下设置好

## 先设置好环境变量（如果还没设置的话）
export JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"
export ANDROID_SDK="/c/Users/wisinzhu/AppData/Local/Android/Sdk"
export ANDROID_NDK="/c/Users/wisinzhu/AppData/Local/Android/Sdk/ndk/26.1.10909125"
export PATH="/c/Program Files/Android/Android Studio/jbr/bin:$PATH" 
export PATH="/c/Program Files/CMake/bin:$PATH"

## 进入构建目录
cd /e/Renderdoc_Git/renderdoc
rm -rf build-android
mkdir build-android
cd build-android

## 运行 cmake，指定 NDK 工具链
cmake -DBUILD_ANDROID=On \
      -DANDROID_ABI=arm64-v8a \
      -DCMAKE_TOOLCHAIN_FILE="$ANDROID_NDK/build/cmake/android.toolchain.cmake" \
      -G "MSYS Makefiles" \
      ..

## 最终编译，使用16核
make -j16