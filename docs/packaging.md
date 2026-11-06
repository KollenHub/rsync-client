# Ubuntu 20.04 打包

工具统一使用 **Nuitka**。发布目标为 Ubuntu 20.04 x86_64 / amd64；不在新版 Linux 上编译后直接声称兼容旧版本。

## 构建

需要 Docker，执行：

```bash
./scripts/build_ubuntu20.sh
```

脚本在 Ubuntu 20.04 容器中构建 Python 3.11.18，安装 `packaging/ubuntu20.04/requirements.txt` 中固定的兼容版本，并先运行全部测试，再执行 Nuitka standalone 编译。构建报告在 `build/ubuntu20.04/compilation-report.xml`。

Qt 使用 `PySide6-Essentials 6.8.3` 的 manylinux_2_28 wheel，兼容 glibc 2.31。不能直接使用当前开发环境的较新 Qt wheel，其最低 glibc 可能高于 Ubuntu 20.04。

产物位于 `dist/`：

- `rsync-desk_1.0.0-3ubuntu20.04_amd64.deb`：安装包，依赖由 apt 处理。
- `rsync-desk-1.0.0-ubuntu20.04-x86_64.tar.gz`：便携目录压缩包，完整解压后运行 `rsync-desk`。
- 同名 `build-info.json`：构建版本、Qt 模块/插件、glibc 审计和冒烟测试结果。
- `SHA256SUMS`：安装包与压缩包校验值。

Python 和应用依赖包含在发布目录内，运行时不需要 Python。rsync 和 OpenSSH 使用系统安装的程序。

## 安装与使用

```bash
sudo apt install ./rsync-desk_1.0.0-3ubuntu20.04_amd64.deb
rsync-desk
```

便携版需要的系统组件：

```bash
sudo apt install rsync openssh-client libgl1 libegl1 libglib2.0-0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-randr0 libxcb-icccm4 libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-sync1 libxcb-xkb1 libx11-xcb1 libfontconfig1 libdbus-1-3 fonts-noto-cjk
tar -xzf rsync-desk-1.0.0-ubuntu20.04-x86_64.tar.gz
cd rsync-desk-1.0.0-ubuntu20.04-x86_64
./rsync-desk
```

需要图形桌面；本版本使用 X11，也可在启用 XWayland 的桌面运行。配置位置仍为 `~/.config/rsync-desk/`，不会覆盖用户原有任务或密码库。

## 体积裁剪

- 不安装 `PySide6-Addons`，不整包收集 `PySide6`。
- Python Qt 扩展只保留 `QtCore`、`QtGui`、`QtWidgets`，排除 `QtTest` 和所有开发/测试工具。
- 排除 WebEngine、QML、Quick、Multimedia、3D、Charts、Pdf、Designer、Help 等未用模块。
- Qt 平台插件只保留 X11 `libqxcb.so` 和诊断所用 `libqoffscreen.so`。不包含 Wayland、图像格式、打印、TLS、主题等未使用插件组。
- 裁剪后分析 ELF 的 DT_NEEDED 依赖链，只删除不可达的额外 Qt 动态库。QtWidgets/X11 间接需要的 QtDBus 等库继续保留。
- 发布目录附带运行依赖的许可证文件。
- 应用图标在窗口、任务栏和桌面入口使用同一套同步箭头标识。运行时使用预渲染 PNG（16–512 像素），不引入 QtSvg；桌面同时安装 SVG 和 hicolor PNG 图标，并刷新图标缓存。

## 验证

- 容器内运行全部源代码测试，覆盖 rsync、SSH 密码、多级跳板机、SFTP、密码库、GUI 和命令预览。
- 审计发布目录所有 ELF，拒绝任何高于 `GLIBC_2.31` 的符号依赖，并检查 `ldd` 缺失库。
- 发布程序移动到包含空格的路径，再执行 `--self-test`，覆盖中文文件同步、密码库、编译后的 SSH_ASKPASS 辅助入口和 Qt 窗口。
- 使用 Xvfb 验证 X11 平台启动，使用干净的 Ubuntu 20.04 运行环境检查安装包。

手动诊断：

```bash
QT_QPA_PLATFORM=offscreen rsync-desk --self-test
```

`SSH_ASKPASS` 通过 `--ssh-askpass` 提前分发到辅助入口，不把编译后的 `sys.executable` 当作 Python 解释器使用，也不会初始化 Qt 或在命令行写入密码。
