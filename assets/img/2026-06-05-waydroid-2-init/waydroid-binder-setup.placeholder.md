# Image Placeholder
Style: manga-minimal
Background: default (#1b1b1e)
Content: Waydroid Binder 驱动探测流程图
- MAINLINE 路径：modprobe binder_linux → binderfs → ioctl BINDER_CTL_ADD → 创建三种binder节点 → ln -s到/dev/
- HALIUM 路径：直接复用宿主 /dev/binder /dev/vndbinder /dev/hwbinder
- 底部标注三组驱动名列表
Status: Codex timed out, regenerate later