---
title: "Waydroid 容器管理——LXC 怎么给 Android 造一个\"假手机\""
author: sanyinchen
date: 2026-06-06
categories: [Android容器, Waydroid源码]
tags: [Waydroid,Android,LXC,容器,Linux]
render_with_liquid: false
toc: true
---

> 图待补：LXC 容器启动全景图：从 lxc-start 到 Android init，设备节点流转、rootfs 挂载全链路（Codex 超时/资源不足，请稍后用 image-generator 重出）

## 接着上次：init 备好了料，这次该开火了

上一篇我把 `waydroid init` 从头到尾拆了一遍——它在你这台陌生机器上摸清硬件、下好镜像、铺好驱动、写好配置，最后留下一堆"半成品"躺在 `/var/lib/waydroid` 里：镜像、空的 rootfs 目录、overlay 目录、一份还没填会话信息的 LXC 配置。

但 init 跑完，安卓其实还没动静。真正把这个"假手机"点亮、让里面的 Android init 进程跑起来的，是 `waydroid session start` 背后的容器启动流程。这篇我们就钻进 `tools/actions/container_manager.py`，看 LXC 是怎么一步步把这些料组装成一个能跑安卓的容器的。

我自己第一次读这块代码时最大的困惑是：LXC 不就是个容器工具吗，`lxc-start` 一敲不就完事了？读完才发现，难的根本不是"启动"那一下，而是启动之前要把环境铺到什么程度——哪些设备要塞进去、文件系统怎么叠、网络怎么搭、权限怎么开。安卓是个挑剔的客人，少给它一样东西它就罢工。所以这篇大半篇幅都在讲"开火前的准备"，真正 `lxc-start` 反而是最轻松的一笔。

先说个关键背景：操作 LXC、装驱动、改设备权限这些都得 root，所以容器这块跑在一个叫 Container Manager 的 root 服务里，对外通过 DBus 暴露 `Start`/`Stop`/`Freeze` 这些方法。你普通用户敲的 `waydroid session start` 其实是隔着 DBus 把请求递给它。这篇聚焦容器本身，DBus 那层就不展开了。

还有个容易忽略的设计前提值得先点一下：Waydroid **完全不自己实现容器**，它从头到尾都是在调系统已经装好的 LXC 命令行工具——`lxc-start`、`lxc-stop`、`lxc-info`、`lxc-freeze`、`lxc-attach`。所以你会看到下面大量代码其实就是在"拼命令行参数 + subprocess 执行"。这是个很聪明的取舍：容器运行时这种又复杂又危险的东西交给成熟的 LXC，Waydroid 只负责当那个懂安卓的"配置师"和"调度员"。理解了这点，再看那些 `["lxc-start", "-P", ..., "--", "/init"]` 的列表就不会觉得奇怪了——它们最终都变成你在 shell 里能敲的命令。

## LXC 配置不是写死的，是"拼"出来的

容器要启动，第一件事是得有一份 LXC 能读懂的配置文件。Waydroid 的配置不是一个静态文件，而是 `set_lxc_config()`（`tools/helpers/lxc.py` 第 143 到 181 行）现场拼出来的。为什么要拼？因为它要同时适配好几个版本的 LXC，还要适配你这台机器独一无二的硬件。

> 图待补：LXC 配置文件的组装流程：config_base → 版本兼容 config_N → config_nodes → config_session 合并（Codex 超时/资源不足，请稍后用 image-generator 重出）

第一步是探测 LXC 版本。`get_lxc_version()`（第 19 行）干得很糙但有效——直接调 `lxc-info --version`，取版本号字符串的第一位数字：

```python
def get_lxc_version(args):
    if shutil.which("lxc-info") is not None:
        command = ["lxc-info", "--version"]
        version_str = tools.helpers.run.user(args, command, output_return=True)
        return int(version_str[0])
    else:
        return 0
```

返回 0 就说明压根没装 LXC，后面会直接报错。

拿到版本号，就开始挑配置片段往一起拼（第 151 到 159 行）：

```python
config_snippets = [ config_paths + "base" ]
if lxc_ver <= 2:
    config_snippets.append(config_paths + "1")
else:
    for ver in range(3, 5):
        snippet = config_paths + str(ver)
        if lxc_ver >= ver and os.path.exists(snippet):
            config_snippets.append(snippet)
```

`config_base` 是地基，所有版本都要。然后看版本：LXC 1/2 这种老古董追加 `config_1`，LXC 3 及以上则依次叠 `config_3`、`config_4`。为什么搞这么麻烦？因为 LXC 在 3.0 前后把一大堆配置项改了名。我把 `config_1` 和 `config_3` 摆一起你就懂了：

```
# config_1（老版本）
lxc.network.type = veth
lxc.aa_profile = unconfined
lxc.init_cmd = /init

# config_3（新版本）
lxc.net.0.type = veth
lxc.apparmor.profile = unconfined
lxc.init.cmd = /init
lxc.no_new_privs = 1
```

`lxc.network.*` 变成了 `lxc.net.0.*`，`lxc.aa_profile` 变成了 `lxc.apparmor.profile`，连 init 命令的写法都改了。Waydroid 用"分片段、按版本拼"的办法优雅地绕开了这堆破事——你装的是哪个版本，就拼对应那几片，互不干扰。`config_4` 里则只有一行 `lxc.seccomp.allow_nesting = 1`，是更新版本才支持的特性，所以单独拆出来。

### config_base 里都写了啥

地基 `config_base` 值得逐行看看（`data/configs/config_base`）：

```
lxc.rootfs.path = /var/lib/waydroid/rootfs
lxc.arch = LXCARCH
lxc.autodev = 0
lxc.cap.keep = audit_control sys_nice wake_alarm setpcap setgid setuid
               sys_ptrace sys_admin block_suspend sys_time net_admin
               net_raw net_bind_service kill dac_override ... sys_chroot
lxc.mount.auto = cgroup:ro sys:ro proc
lxc.console.path = none
lxc.include = /var/lib/waydroid/lxc/waydroid/config_nodes
lxc.include = /var/lib/waydroid/lxc/waydroid/config_session
lxc.hook.post-stop = /dev/null
```

`lxc.rootfs.path` 指明容器的根就是那个 `rootfs` 目录；`lxc.arch = LXCARCH` 里的 `LXCARCH` 是个占位符，等会儿会被 sed 替换成真实架构。`lxc.autodev = 0` 是说"别帮我自动建 /dev"，因为 Waydroid 要自己精确控制塞哪些设备进去。

最有意思的是 `lxc.cap.keep` 那一长串。注意是 `keep` 不是 `drop`——它走白名单策略：默认把所有 Linux capability 全丢光，只保留这里列的这些。安卓确实需要不少特权，比如 `net_admin`（配网络）、`sys_nice`（调度优先级）、`sys_time`（设时间）、`wake_alarm`（定时唤醒），但像加载内核模块这种没列进来的能力，容器里的安卓压根拿不到。白名单比黑名单安全，因为以后内核新增的危险能力默认就是被拒的。

`lxc.mount.auto = cgroup:ro sys:ro proc` 让 LXC 自动把 cgroup、sysfs 以只读方式、procfs 挂进容器——安卓启动要读这些。最后两行 `lxc.include` 是关键的伏笔：把 `config_nodes`（设备节点）和 `config_session`（会话相关）包含进来，这俩才是真正动态生成的大头。

### 收尾：替换架构、拷 seccomp、配 AppArmor

片段拼成一个完整 config 之后还有几道收尾（第 163 到 171 行）：先 `cat` 把所有片段合成一个文件，再用 sed 把 `LXCARCH` 换成 `platform.machine()` 的真实值，然后把 seccomp 系统调用过滤规则拷过去。

AppArmor 那段是动态的：

```python
if get_apparmor_status(args):
    command = ["sed", "-i", "-E",
               "/lxc.aa_profile|lxc.apparmor.profile/ s/unconfined/{}/g".format(LXC_APPARMOR_PROFILE),
               lxc_path + "/config"]
```

`get_apparmor_status()`（第 130 行）会查三件事：`aa-enabled` 命令、systemd 里 apparmor 服务是否 active、以及 `lxc-waydroid` 这个 profile 有没有真的加载进内核。三个条件都满足才动手，把配置里默认的 `unconfined` 全替换成真正的 profile 名。能上 AppArmor 的环境就上，上不了保持 unconfined，绝不因为环境不具备就卡住报错——这种"有则加固、无则不强求"的写法，在 Waydroid 里到处都是。

## 设备节点映射：把宿主机的硬件一件件递进去

> 图待补：设备节点映射图：宿主机 /dev 下 binder/graphics/dri/input/video/framebuffer/dma_heap 等设备如何 bind mount 进容器（Codex 超时/资源不足，请稍后用 image-generator 重出）

到了我觉得整个容器管理里最精彩的部分：`generate_nodes_lxc_config()`（第 40 到 127 行）。它生成的就是前面那个 `config_nodes` 文件，内容是一长串 `lxc.mount.entry`——本质上是告诉 LXC："把宿主机的这些设备，bind mount 进容器里去。"

为什么非得这么干？因为前面 `config_base` 里写了 `lxc.autodev = 0`，容器启动时 `/dev` 是空的。安卓要用的每一个设备节点，都得 Waydroid 亲手挂进去。挂多了有安全隐患，挂少了安卓罢工，所以这份清单是精心挑过的。

先看生成每条目的辅助函数 `add_node_entry()`（第 27 行）和包了一层的 `make_entry`：

```python
def make_entry(src, dist=None, mnt_type="none",
               options="bind,create=file,optional 0 0", check=True):
    return add_node_entry(nodes, src, dist, mnt_type, options, check)
```

每条 entry 由几部分组成：`src`（宿主机上的路径）、`dist`（容器里的目标路径，不填就默认去掉开头的 `/`）、`mnt_type`（挂载类型）、`options`（挂载选项）。默认选项里那个 `optional` 很关键——意思是"这设备存在就挂、不存在就跳过，别报错"。`check=True` 还会先 `os.path.exists` 验一下。这两个加起来，就让这份清单可以放心地"广撒网"：列上各种可能的设备，存在的精确投喂，不存在的自动忽略。

### 必需的基础节点

```python
make_entry("tmpfs", "dev", "tmpfs", "nosuid 0 0", False)
make_entry("/dev/zero")
make_entry("/dev/null")
make_entry("/dev/full")
make_entry("/dev/ashmem")
make_entry("/dev/fuse")
make_entry("/dev/ion")
make_entry("/dev/tty")
```

第一行先在容器里挂个 tmpfs 当 `/dev`（这就是为什么 autodev 关掉也没事，Waydroid 自己铺）。然后是 `/dev/zero`、`/dev/null`、`/dev/full` 这些任何 Unix 系统都离不开的基础字符设备。`/dev/ashmem`（匿名共享内存）和 `/dev/ion`（ION 内存分配器）是安卓特有的内存机制，`/dev/fuse` 给用户态文件系统用。

### 图形相关：让安卓直接摸到你的 GPU

这部分是 Waydroid 性能接近原生的命脉：

```python
make_entry("/dev/kgsl-3d0")     # 高通 Adreno GPU
make_entry("/dev/mali0")        # ARM Mali GPU
make_entry("/dev/pvr_sync")     # PowerVR
render, _ = tools.helpers.gpu.getDriNode(args)
make_entry(render)              # DRI 渲染节点
for n in glob.glob("/dev/fb*"):       # framebuffer
    make_entry(n)
for n in glob.glob("/dev/video*"):    # 摄像头/编解码
    make_entry(n)
for n in glob.glob("/dev/dma_heap/*"):
    make_entry(n)
```

它把各家 GPU 的节点都列上：高通的 `kgsl-3d0`、ARM 的 `mali0`、PowerVR 的 `pvr_sync`，反正不存在的会自动跳过。最通用的是那个 `getDriNode(args)`（`gpu.py` 第 20 行），它会去 `/dev/dri/renderD*` 里挑一个内核驱动受支持的渲染节点——这是绝大多数 Linux 桌面（Intel/AMD/Nvidia 开源驱动）的情况。framebuffer、video、dma_heap 这些直接 `glob` 通配一把全收。安卓拿到这些节点，就能直接调你的真实 GPU 做硬件渲染，而不是退到龟速的软件渲染。

### Binder：安卓的生命线

```python
make_entry("/dev/" + args.BINDER_DRIVER, "dev/binder", check=False)
make_entry("/dev/" + args.VNDBINDER_DRIVER, "dev/vndbinder", check=False)
make_entry("/dev/" + args.HWBINDER_DRIVER, "dev/hwbinder", check=False)
```

安卓几乎所有跨进程通信都走 binder，没它系统起都起不来。注意这里 `src` 用的是 `args.BINDER_DRIVER`——这是 init 时探测出来的真实节点名（可能是 `binder`，也可能是 `anbox-binder` 这种避让用的马甲名），而 `dist` 固定写成 `dev/binder`。也就是说，不管宿主机上那个节点叫什么怪名字，映射进容器后安卓看到的永远是标准的 `/dev/binder`。`check=False` 是因为这些节点的存在性前面驱动那步已经保证过了。

### ADB、网络和其它

```python
make_entry("none", "dev/pts", "devpts", "defaults,mode=644,ptmxmode=666,create=dir 0 0", False)
make_entry("/dev/uhid")
make_entry("/dev/net/tun", "dev/tun")
```

`/dev/pts` 是伪终端，`adb shell` 要用；`/dev/uhid` 是给 ADB 模拟输入设备用的；`/dev/net/tun` 则是 VPN 类应用建立 TUN/TAP 隧道必需的。再往后还有 `/dev/sw_sync`（HWC 硬件合成器要的同步机制）、振动器、Mediatek 专用的几个媒体节点等等，照例都是"有就挂没就跳"。

除了 `/dev` 下的设备，它还挂了一些 sysfs 路径和 tmpfs。比如把 `/sys/kernel/debug` 递归 bind 进去（HWC 和一些图形调试要读）、把低内存杀手的 `/sys/module/lowmemorykiller` 挂进去；又在容器里好几个挂载点上铺 tmpfs：

```python
make_entry("tmpfs", "tmp", "tmpfs", "nodev 0 0", False)
make_entry("tmpfs", "var", "tmpfs", "nodev 0 0", False)
make_entry("tmpfs", "run", "tmpfs", "nodev 0 0", False)
```

给 `/tmp`、`/var`、`/run` 铺 tmpfs，是因为这些目录在只读的 system 镜像里可能不可写，铺一层内存文件系统上去就能写了，且重启即清空。还有一行把宿主的 host-permissions 目录挂进容器的 `vendor/etc/host-permissions`——这正是上一篇 init 时 `setup_host_perms` 拷进去的那些 NFC、红外权限声明文件，在这里被递进容器，让安卓知道"这台设备有哪些硬件"。一份设备清单里，从字符设备到 sysfs 到 tmpfs 到权限文件，方方面面都安排到了。

### HALIUM 设备的特殊待遇

如果你不是普通桌面而是个 Halium 设备（`vendor_type != "MAINLINE"`），还要多挂一些（第 79 到 82 行）：

```python
if args.vendor_type != "MAINLINE":
    if not make_entry("/dev/hwbinder", "dev/host_hwbinder"):
        raise OSError('Binder node "hwbinder" of host not found')
    make_entry("/vendor", "vendor_extra", options="rbind,optional 0 0")
```

把宿主的 `/dev/hwbinder` 映射成容器里的 `host_hwbinder`，再把整个 `/vendor` 递归 bind 进去。因为 Halium 设备的 HAL 就指着用宿主现成的那一套，得让容器能摸到。最后还有几行是 WSLg（Windows 上跑的 Linux）兼容，把 `/mnt/wslg` 挂进去——可见 Waydroid 连"在 Windows 的 WSL 里跑安卓"这种套娃场景都考虑到了。

## OverlayFS：只读镜像 + 可写层 = 看起来能写的 rootfs

> 图待补：OverlayFS 三层结构：只读 system.img/vendor.img 作为 lower，overlay_rw 作为 upper，合并为 rootfs（Codex 超时/资源不足，请稍后用 image-generator 重出）

设备节点搞定，接下来是文件系统。`mount_rootfs()`（`tools/helpers/images.py` 第 162 到 204 行）负责把镜像变成容器能用的根。最直白的两行是这样：

```python
helpers.mount.mount(args, images_dir + "/system.img",
                    tools.config.defaults["rootfs"], umount=True)
...
helpers.mount.mount(args, images_dir + "/vendor.img",
                    tools.config.defaults["rootfs"] + "/vendor")
```

先把 `system.img` 挂到 `rootfs`，再把 `vendor.img` 挂到 `rootfs/vendor`。但这俩镜像是**只读**的，问题来了——安卓运行时总要往系统目录写点东西吧（缓存、临时文件、运行时生成的配置）？如果系统是只读的，安卓直接崩。

这就是 OverlayFS 登场的地方。它的核心思想特别巧：把一个只读的"下层"和一个可写的"上层"叠在一起，对外呈现成一个看起来完全可读写的目录。你读文件时优先看上层、上层没有就穿透到下层；你写文件时一律写到上层，下层的原始镜像纹丝不动。看代码（第 166 到 172 行）：

```python
if cfg["waydroid"]["mount_overlays"] == "True":
    helpers.mount.mount_overlay(args,
        [tools.config.defaults["overlay"], tools.config.defaults["rootfs"]],
        tools.config.defaults["rootfs"],
        upper_dir=tools.config.defaults["overlay_rw"] + "/system",
        work_dir=tools.config.defaults["overlay_work"] + "/system")
```

OverlayFS 要三个东西，对应磁盘上三个目录：

- **lower（下层，只读）**：这里是 `overlay` 目录加上已经挂好镜像的 `rootfs`。镜像的内容在这层，永远不会被改。
- **upper（上层，可写）**：`overlay_rw/system`。安卓运行时所有的写入都落到这儿。
- **work（工作目录）**：`overlay_work/system`。这个目录一般人会忽略，它是 OverlayFS 内部用来做"原子操作"的暂存区——比如你改一个下层的文件，内核需要先把它复制到上层（copy-up），这个中间过程就在 work 目录里完成，保证操作要么成功要么干净回滚。它必须和 upper 在同一个文件系统上。

vendor 镜像也叠一套一模一样的 overlay（第 180 到 185 行），upper 和 work 分别是 `overlay_rw/vendor`、`overlay_work/vendor`。

这套三层结构带来两个大好处。一是原始镜像永远干净，你在安卓里折腾再狠，`system.img` 本身一个字节都没变。二是 OTA 升级时可以把底层镜像整个换掉，而你在上层的改动还能保留——这就是为什么 Waydroid 升级体验是无缝的。如果哪台机器 OverlayFS 挂不起来（有些内核配置不支持），代码会捕获异常、把 `mount_overlays` 关掉并存回配置，下次就老老实实直接用镜像（第 173 到 176 行），又是一个"不支持就降级"的兜底。

`mount_rootfs` 末尾还有几笔收尾，看代码（第 187 到 201 行）：

```python
for egl_path in ["/vendor/lib/egl", "/vendor/lib64/egl"]:
    if os.path.isdir(egl_path):
        helpers.mount.bind(args, egl_path, tools.config.defaults["rootfs"] + egl_path)
if helpers.mount.ismount("/odm"):
    helpers.mount.bind(args, "/odm", tools.config.defaults["rootfs"] + "/odm_extra")
...
make_prop(args, session, args.work + "/waydroid.prop")
helpers.mount.bind_file(args, args.work + "/waydroid.prop",
                        tools.config.defaults["rootfs"] + "/vendor/waydroid.prop")
```

如果宿主有 `/vendor/lib/egl`、`/vendor/lib64/egl` 就 bind 进容器，让安卓直接用上宿主的 EGL 图形库；有独立的 odm 分区就挂成 `odm_extra`。最后生成一份 `waydroid.prop`（`make_prop` 第 129 行，会把屏幕 DPI、Wayland display 这些会话相关的属性写进去），再 bind 成容器里的 `/vendor/waydroid.prop`——这是这次会话专属的属性文件，安卓启动时会读它，所以每次 session 的 DPI、显示配置都能不一样。这也解释了为什么这步要在 `do_start` 里、拿到 `session` 之后才做，而不是 init 时一次性搞定。

## do_start：把上面这些按正确顺序串起来

前面讲的配置生成、节点映射、rootfs 挂载都是"零件"，真正把它们按顺序组装、点火的是 `do_start()`（`container_manager.py` 第 153 到 221 行）。我按它的执行顺序走一遍：

```python
def do_start(args, session):
    ...
    prepare_drivers_once(args)            # 1. 驱动

    command = [tools.config.tools_src + "/data/scripts/waydroid-net.sh", "start"]
    tools.helpers.run.user(args, command) # 2. 网络

    if which("waydroid-sensord"):         # 3. 传感器
        tools.helpers.run.user(args, ["waydroid-sensord", ...], output="background")

    # 4. cgroup / NFC hack（省略）
    set_permissions(args)                 # 5. 设备权限

    helpers.lxc.generate_session_lxc_config(args, session)  # 6. 会话配置
    cfg = tools.config.load(args)
    helpers.images.mount_rootfs(args, cfg["waydroid"]["images_path"], session)  # 7. 挂 rootfs
    helpers.protocol.set_aidl_version(args)  # 8. AIDL 协议版本

    helpers.lxc.start(args)               # 9. lxc-start！
    services.hardware_manager.start(args) # 10. 硬件服务
    args.session = session
```

**第一步，驱动。** `prepare_drivers_once()`（第 134 到 151 行）：

```python
prepared_drivers = False
def prepare_drivers_once(args):
    global prepared_drivers
    if prepared_drivers:
        return
    cfg = tools.config.load(args)
    if cfg["waydroid"]["vendor_type"] == "MAINLINE":
        if helpers.drivers.probeBinderDriver(args) != 0:
            logging.error("Failed to load Binder driver")
        helpers.drivers.probeAshmemDriver(args)
    helpers.drivers.loadBinderNodes(args)
    set_permissions(args, [
        "/dev/" + args.BINDER_DRIVER,
        "/dev/" + args.VNDBINDER_DRIVER,
        "/dev/" + args.HWBINDER_DRIVER
    ], "666")
    prepared_drivers = True
```

名字里带 `once` 是因为开头那个全局标志位 `prepared_drivers`——一次会话只跑一遍，重复调直接返回。只有 MAINLINE 模式才需要现场加载 binder 驱动（`probeBinderDriver` 会去 `modprobe binder_linux` 或挂 binderfs）和 ashmem 驱动，因为 Halium 设备这些是现成的。最后 `loadBinderNodes` 从配置里读回 init 时探测好的节点名，再把这三个 binder 节点单独 chmod 成 666（注意这次是 666 不是 777，binder 节点不需要执行位）。binder 是安卓的命根子，所以它排在所有准备工作的第一位——没它，后面挂得再齐安卓也起不来。

**第二步，网络。** 这步调的是 `data/scripts/waydroid-net.sh`，一个独立的 shell 脚本，干的事跟 Docker 的桥接网络几乎一模一样。我把它的关键几步拎出来：

```sh
# 建网桥
[ ! -d /sys/class/net/${LXC_BRIDGE} ] && ip link add dev ${LXC_BRIDGE} type bridge
# 给网桥配网关地址 192.168.240.1
ip addr add ${CIDR_ADDR} broadcast + dev ${LXC_BRIDGE}
ip link set dev ${LXC_BRIDGE} up
# 拉起 dnsmasq 发 DHCP
dnsmasq ... --listen-address ${LXC_ADDR} --dhcp-range ${LXC_DHCP_RANGE} ...
# NAT
$IPTABLES_BIN -t nat -A POSTROUTING -s ${LXC_NETWORK} ! -d ${LXC_NETWORK} -j MASQUERADE
```

`LXC_BRIDGE` 就是 `waydroid0`，网关地址 `192.168.240.1`（脚本第 20 行 `LXC_ADDR`），整个容器网段是 `192.168.240.0/24`。它拉起一个 `dnsmasq` 进程给容器发 DHCP（地址池 `192.168.240.2` 到 `.254`），容器里的安卓一开机就能自动拿到一个 IP。最后那条 iptables MASQUERADE 规则是做 NAT——把容器发出去的包源地址伪装成宿主机的，这样容器才能借宿主的网卡上外网，回包也能正确转回来。脚本还很周到地优先用 `iptables-legacy`、且对 nftables-only 的系统准备了 `nft` 版本的等价规则（第 124 行那段）。前面设备节点那节里 `config_3` 的 `lxc.net.0.link = waydroid0` 就是把容器的 `eth0` 这头接到这个网桥上，两头一接，容器就联网了。

**第三到五步，零碎但必要。** 这几步看着杂，其实每一条都是踩过坑留下的。

先是传感器：如果系统里装了 `waydroid-sensord`，就把它拉起来（`output="background"` 后台跑），它负责把宿主机的传感器数据转发给容器里的安卓。

然后是 cgroup 的 schedtune 兼容（第 180 到 191 行），这段代码乍看莫名其妙：

```python
if os.path.ismount("/sys/fs/cgroup/schedtune"):
    try:
        os.mkdir("/sys/fs/cgroup/schedtune/probe0")
        os.mkdir("/sys/fs/cgroup/schedtune/probe0/probe1")
    except OSError:
        command = ["umount", "-l", "/sys/fs/cgroup/schedtune"]
        tools.helpers.run.user(args, command, check=False)
    finally:
        # 清理掉探测用的临时目录
        ...
```

它其实是在"试探"——安卓用 schedtune 这个 cgroup 子系统做性能调度，但需要它支持嵌套（能在里面再建子组）。这段代码就建俩临时目录 `probe0/probe1` 探一下：建得成说明支持嵌套，建不成（抛 OSError）就干脆把 schedtune 卸载掉，免得安卓用了反而出错。探完无论成败都把临时目录删干净。一个很典型的"运行时探测能力、不行就绕开"的写法。

接着是 NFC hack（第 194 到 199 行）：停掉宿主机的 `nfcd` 服务。因为 NFC 硬件同一时刻只能被一个进程独占，宿主和容器抢的话谁都用不了，所以容器启动时先把宿主的让出来（停容器时再恢复）。代码还贴心地分了 `start`/`stop` 命令（Ubuntu Touch 那套）和 `systemctl`（systemd）两种情况。

最后是设备权限 `set_permissions()`（第 68 到 105 行），它把一长串设备节点 chmod 成 777：

```python
perm_list = [
    "/dev/ashmem", "/dev/sw_sync", "/dev/Vcodec", "/dev/MTK_SMI",
    "/dev/graphics", "/dev/pvr_sync", "/dev/ion",
]
perm_list.extend(glob.glob("/dev/dri/renderD*"))   # DRM 渲染节点
perm_list.extend(glob.glob("/dev/fb*"))            # framebuffer
perm_list.extend(glob.glob("/dev/video*"))         # video
perm_list.extend(glob.glob("/dev/dma_heap/*"))     # DMA-BUF heaps
```

为什么要开 777？因为容器里的安卓进程是以它自己的 uid 跑的，跟宿主机上拥有这些设备的用户（通常是 root 或 video 组）对不上号。最省事的办法就是把权限放到最开，让安卓怎么都能访问。这在安全上确实不算优雅，但 Waydroid 靠前面的命名空间和 capability 限制兜底，权限放开的影响被框在容器里。注意它和前面"设备节点映射"是两件事：那边是把设备 bind 进容器，这边是改这些设备在宿主机上的权限位——得两样都做对，安卓才真能用上。

**第六步，会话配置。** `generate_session_lxc_config()`（`lxc.py` 第 183 行）填充之前那个空的 `config_session` 文件。它把和你当前登录会话强相关的东西 bind 进容器。看它内部那个 `make_entry`（第 185 到 193 行）就明白为什么这部分要单独拆出来：

```python
def make_entry(src, dist=None, mnt_type="none", options="rbind,create=file 0 0"):
    if any(x in src for x in ["\n", "\r"]):
        logging.warning("User-provided mount path contains illegal character: " + src)
        return False
    if dist is None and (not os.path.exists(src) or
                         str(os.stat(src).st_uid) != session["user_id"]):
        logging.warning("User-provided mount path is not owned by user: " + src)
        return False
    return add_node_entry(...)
```

注意它做了两道安全校验：一是路径里不能有换行符（防注入），二是要挂的东西必须是**当前用户拥有的**（`st_uid` 和 `session["user_id"]` 比对）。这俩检查是必须的——因为这些路径来自用户会话（Wayland socket 在哪、数据目录在哪都是用户那边传过来的），而容器管理器是 root 权限跑的，要是不校验，等于给了普通用户一个"让 root 帮我把任意文件挂进容器"的口子。

具体挂哪些呢？先在容器里建好 `XDG_RUNTIME_DIR` 挂载点，然后把宿主的 Wayland socket 映射进容器（安卓画面就渲染到这个 socket，再贴到你桌面上），接着是 PulseAudio 的 socket（声音），最后把你的用户数据目录 rbind 成容器里的 `/data`（你装的 App、登的账号都存这）。这些都填进 `config_session`，等 `lxc-start` 时和前面的 `config_nodes`、`config_base` 一起被 LXC 读进去。

**第七、八步，文件系统和协议。** 挂 rootfs（上一节讲过），然后 `set_aidl_version()`（`protocol.py` 第 6 行）。这步挺有意思：它去读刚挂好的 `system/build.prop` 里的 `ro.build.version.sdk`，也就是这个安卓镜像的 API level，然后根据版本号决定 binder 和 servicemanager 用哪个 AIDL 协议版本：

```python
if android_api < 28:
    binder_protocol = "aidl";  sm_protocol = "aidl"
elif android_api < 30:
    binder_protocol = "aidl2"; sm_protocol = "aidl2"
...
else:
    binder_protocol = "aidl3"; sm_protocol = "aidl6"
```

不同 Android 版本的 binder 通信协议有差异，gbinder 库得知道用哪个版本才能跟容器里的安卓对上话。注意函数开头那行注释 `# Call me with rootfs mounted!`——它必须在 rootfs 挂好之后调，因为得读镜像里的 build.prop，所以顺序上排在挂载之后。

**第九步，点火。** 终于到 `helpers.lxc.start()`（第 400 行）：

```python
command = ["lxc-start", "-P", tools.config.defaults["lxc"],
           "-F", "-n", "waydroid", "--", "/init"]
tools.helpers.run.user(args, command, output="background")
wait_for_running(args)
```

`-n waydroid` 指定容器名，`-F` 是前台运行（配合 `output="background"` 把它丢到后台进程里），最后那个 `/init` 是关键——它告诉 LXC，容器里的 1 号进程用 `/init`，也就是安卓自己的 init。这一刻，前面铺好的所有设备、文件系统、网络全部就位，安卓 init 在这个隔离环境里正式开机，接管后面的 zygote、system_server 一整套启动流程。

`wait_for_running()`（第 388 行）则在外面轮询：

```python
def wait_for_running(args):
    lxc_status = status(args)
    timeout = 10
    while lxc_status != "RUNNING" and timeout > 0:
        lxc_status = status(args)
        logging.info("waiting {} seconds for container to start...".format(timeout))
        timeout = timeout - 1
        time.sleep(1)
    if lxc_status != "RUNNING":
        raise OSError("container failed to start")
```

最多等 10 秒，每秒查一次状态，到点还不是 `RUNNING` 就抛错。`start()` 末尾还有个小彩蛋——`lxc-start` 会把日志文件的权限改成 700，它补一句 `os.chmod(args.log, 0o666)` 给改回来，免得普通用户后面 `waydroid log` 读不了日志。这种"修别人留下的坑"的小细节，是读真实项目代码才能见到的。

顺带提一句 seccomp。前面拼配置时拷进去的那份 `waydroid.seccomp`，会在这一步随容器启动生效，在系统调用层面再加一道过滤——哪些 syscall 容器能调、哪些直接拦掉。它和命名空间、capability、AppArmor 叠在一起，构成了"视野隔离 + 权限收紧 + 系统调用过滤"的多层防护。`config_4` 里那行 `lxc.seccomp.allow_nesting = 1` 则是允许容器内再嵌套 seccomp 规则，是新版 LXC 才有的能力，所以单独拆成一片按版本加载。

## 容器跑起来之后：四个状态之间怎么转

> 图待补：容器生命周期状态机：STOPPED → RUNNING → FROZEN → RUNNING → STOPPED 的状态转换（Codex 超时/资源不足，请稍后用 image-generator 重出）

容器不是只有"开"和"关"。Waydroid 给它定义了一个小状态机，状态靠 `lxc-info` 查（`status()`，第 380 行）：

```python
def status(args):
    command = ["lxc-info", "-P", tools.config.defaults["lxc"], "-n", "waydroid", "-sH"]
    try:
        return tools.helpers.run.user(args, command, output_return=True).strip()
    except Exception:
        logging.info("Couldn't get LXC status. Assuming STOPPED.")
        return "STOPPED"
```

`-sH` 是只输出状态那一栏、不带表头，拿到的就是 `RUNNING`/`FROZEN`/`STOPPED` 这种纯字符串。查不到就保守地当成 STOPPED——又是一处"出错往安全的方向兜"。核心是四态流转：

**STOPPED → RUNNING：start。** 就是上面 `do_start` 那一整套。

**RUNNING → FROZEN：freeze。** 这个我特别想夸一下。`freeze()`（`container_manager.py` 第 282 行）最终调 `lxc-freeze`，利用 cgroup 的 freezer 子系统，把容器里所有进程**一把全部暂停**——注意是暂停不是杀死，进程定格在原地，CPU 占用归零，但内存状态原封不动保留着。等你需要时 `unfreeze` 一下，瞬间满血复活，安卓根本不知道自己刚被"时间静止"过。这就是为什么 Waydroid 后台挂着几乎不耗电：不用的时候它被冻成一尊雕像了。笔记本合盖休眠默认走的就是 freeze（在硬件管理服务的 `suspend` 回调里）。

**FROZEN → RUNNING：unfreeze。** `unfreeze()`（第 291 行）调 `lxc-unfreeze` 解冻，调用后还会循环等状态真的从 FROZEN 变回来才返回。冻结和解冻这一对，是 Waydroid 能"常驻后台又不拖累系统"的关键——你点开安卓 App 时它毫秒级唤醒，你切走一会它就被冻住、CPU 占用归零，体验上跟手机锁屏息屏几乎一样。

**RUNNING → STOPPED：stop。** `stop()`（第 223 行）的顺序基本是 `do_start` 倒着来，把铺过的东西一样样收干净：

```python
services.hardware_manager.stop(args)
status = helpers.lxc.status(args)
if status != "STOPPED":
    helpers.lxc.stop(args)
    while helpers.lxc.status(args) != "STOPPED":
        pass
# 拆网络
command = [tools.config.tools_src + "/data/scripts/waydroid-net.sh", "stop"]
tools.helpers.run.user(args, command, check=False)
# 恢复 nfcd、杀掉 sensord、卸载 rootfs ...
helpers.images.umount_rootfs(args)
```

先停 hardware_manager，再 `lxc.stop()`（底层是 `lxc-stop -n waydroid -k`，`-k` 是 kill，不等优雅退出直接杀）。注意那个 `while ... pass` 的忙等——它要确认容器状态真的变成 STOPPED 才往下走，因为后面卸载 rootfs 不能在容器还活着的时候做，否则设备占用会报错。同样的忙等模式在 `freeze`/`unfreeze` 里也有（确认状态切换完成）。停掉容器后，调 `waydroid-net.sh stop` 拆掉网桥、杀掉 dnsmasq、删掉 iptables 规则，把之前停掉的 nfcd 恢复回来，杀掉 `waydroid-sensord`，最后 `umount_rootfs` 把那一摞 overlay 和镜像挂载全部卸干净。整个 `stop` 还套在一个大 `try/except` 里——收尾过程中哪一步出错都不至于让整个停止流程崩掉。

stop 最后还有一笔：如果当前还跟着一个 session（`"session" in args`），且调用方要求连 session 一起退，它会给那个 session 进程发 `SIGUSR1` 信号通知它"容器没了，你也收摊吧"。这就是容器和会话两个进程之间的协同——容器这边被停了，要主动告诉会话那边一声。

**restart = stop + start。** `restart()`（第 274 行）没什么玄机，就是先 stop 再 start。

理解了 freeze 这个机制，你再看 `waydroid app list` 那段代码（它会临时 unfreeze 读完应用列表再 freeze 回去）就明白它在省电上有多抠了。值得一提的是，休眠到底是 freeze 还是直接 stop 是可配置的——`waydroid.cfg` 里的 `suspend_action` 字段，默认 `freeze`（省电又秒恢复），但你也可以改成 `stop`（彻底关掉，更省内存但下次要重新开机）。这种"给个合理默认、又留个口子让你按需调"的态度，贯穿了整个 Waydroid。

另外这四个状态不光是内部用，命令行也直接开放：`waydroid container start/stop/restart/freeze/unfreeze` 一一对应（需要 root），调试时想手动把容器定格或解冻，敲这几条就行。

## 最后一块拼图：容器内的 Android 环境变量

讲完启停，还有个细节值得一提：当你 `waydroid shell` 进容器、或者 Waydroid 内部要在容器里跑命令时，得给进程一套正确的安卓环境变量，否则 `pm`、`am` 这些安卓命令根本找不到自己的库。这套环境定义在 `lxc.py` 第 423 行的 `ANDROID_ENV`：

```python
ANDROID_ENV = {
    "PATH": "/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:...",
    "ANDROID_ROOT": "/system",
    "ANDROID_DATA": "/data",
    "ANDROID_ART_ROOT": "/apex/com.android.art",
    "BOOTCLASSPATH": "/apex/com.android.art/javalib/core-oj.jar:...",
    ...
}
```

`ANDROID_ROOT`、`ANDROID_DATA` 这些是安卓系统约定俗成的目录指向，`BOOTCLASSPATH` 则是一长串 Java 核心库的路径——安卓的 Java 运行时启动时要靠它定位框架类。但光有这些静态的还不够，`android_env_attach_options()`（第 435 行）还会**动态地**从运行中的容器里捞两个变量：

```python
command = ["lxc-attach", "-P", ..., "-n", "waydroid", "--clear-env", "--",
           "/system/bin/cat", "/data/system/environ/classpath"]
