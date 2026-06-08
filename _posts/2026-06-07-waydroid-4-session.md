---
title: "Waydroid 会话与图形——Session、Wayland 和跨系统服务的协作"
author: sanyinchen
date: 2026-06-07
categories: [Android容器, Waydroid源码]
tags: [Waydroid,Android,Wayland,DBus,Session]
render_with_liquid: false
toc: true
---

> 图待补：Waydroid Session 启动全景：从 waydroid session start → 环境检测 → DBus 通信 → 容器启动 → 服务注册 → App 显示（Codex 超时/资源不足，请稍后用 image-generator 重出）

## 容器起来了，可它怎么知道画在哪、声音往哪送

前两篇我把 `waydroid init` 和容器启动都拆过了。到上一篇结尾，LXC 已经把安卓 `/init` 拉起来，容器在那六道命名空间围成的隔离间里正式开机了。但你有没有想过一个问题——这个安卓系统跑在一个隔离的容器里，它渲染出来的画面，凭什么能出现在我的 Linux 桌面上？我在安卓里复制一段文字，凭什么能粘贴到我的 Firefox？安卓 App 弹的通知，凭什么会出现在我 KDE 的通知中心？

容器是隔离的，但桌面体验要的恰恰是"打通"。这中间的活，全是 Session（会话）这一层干的。这篇我们就钻进 `tools/actions/session_manager.py`，看看一个普通用户身份的 session 进程，是怎么把"隔离的容器"和"我的桌面"缝合到一起的。

先点一个跟前两篇最大的不同：容器那一层是 root 跑的（要装驱动、操作 LXC），但 session 这一层是**你这个普通用户**跑的。为什么？因为它要碰的全是用户级的东西——你的 Wayland 显示、你的剪贴板、你的桌面通知、你家目录下的数据。这些东西 root 反而不该随便碰。于是 Waydroid 就把活拆成两半：root 的 Container Manager 管容器本身，普通用户的 Session Manager 管桌面集成，两边通过 DBus 对话。这篇的主角就是后面这半。

## session start 的第一件事：体检环境

入口是 `start()` 函数（`session_manager.py` 第 40 行）。它干的第一件正经事不是急着启动，而是**体检**——确认你这个图形环境到底能不能跑安卓。毕竟安卓画面最后要渲染到 Wayland 上，要是连 Wayland 都没有，启动了也是白搭。

一上来先抢 DBus 名字（第 42 行）：

```python
try:
    _name = dbus.service.BusName("id.waydro.Session", dbus.SessionBus(), do_not_queue=True)
except dbus.exceptions.NameExistsException:
    logging.error("Session is already running")
    ...
    return
```

注意这里用的是 `dbus.SessionBus()`——会话总线，跟容器那边用系统总线是两套。抢名字 `id.waydro.Session` 失败，说明已经有一个 session 在跑了，直接退出。这是个很简洁的"单例"实现，靠 DBus 名字的唯一性天然保证一个用户同时只有一个 session。

抢到名字之后，开始查 Wayland（第 50 到 67 行），这段是整个体检的核心：

```python
wayland_display = session["wayland_display"]
if wayland_display == "None" or not wayland_display:
    logging.warning('WAYLAND_DISPLAY is not set, defaulting to "wayland-0"')
    wayland_display = session["wayland_display"] = "wayland-0"

if os.path.isabs(wayland_display):
    wayland_socket_path = wayland_display
else:
    xdg_runtime_dir = session["xdg_runtime_dir"]
    if xdg_runtime_dir == "None" or not xdg_runtime_dir:
        logging.error("XDG_RUNTIME_DIR is not set; please don't start a Waydroid session with 'sudo'!")
        sys.exit(1)
    wayland_socket_path = os.path.join(xdg_runtime_dir, wayland_display)
if not os.path.exists(wayland_socket_path):
    logging.error(f"Wayland socket '{wayland_socket_path}' doesn't exist; are you running a Wayland compositor?")
    sys.exit(1)
```

一步步看。先读 `WAYLAND_DISPLAY` 环境变量，这玩意儿告诉程序"Wayland 的 socket 叫什么名"。没设的话，它不直接报错，而是友好地默认成 `wayland-0`（绝大多数 Wayland 合成器的默认名），打个 warning 就接着走。

接着拼 socket 的完整路径。如果 `WAYLAND_DISPLAY` 本身就是绝对路径，直接用；否则得拼上 `XDG_RUNTIME_DIR`（通常是 `/run/user/1000` 这种）。这里有个特别贴心的报错——如果 `XDG_RUNTIME_DIR` 没设，它专门提示你"别用 sudo 启动 session！"。为什么？因为一旦你 `sudo`，环境就变成 root 的了，`XDG_RUNTIME_DIR` 这种用户级变量就丢了。这个错误信息背后，是无数人踩过 `sudo waydroid session start` 这个坑、然后一脸懵的血泪。

最后 `os.path.exists` 确认这个 socket 文件真实存在。不存在就说明你压根没跑 Wayland 合成器（或者跑的是纯 X11），直接 `sys.exit(1)`。体检不过，绝不硬上。

## session 字典：把"你是谁"打包好

体检的同时，`start()` 在维护一个叫 `session` 的字典。它从哪来？`config/__init__.py` 第 57 行的 `session_defaults`，里面装着当前这次会话的全部身份信息：

```python
session_defaults = {
    "user_name": pwd.getpwuid(os.getuid()).pw_name,
    "user_id": str(os.getuid()),
    "pid": str(os.getpid()),
    "xdg_data_home": str(os.environ.get('XDG_DATA_HOME', ...)),
    "xdg_runtime_dir": str(os.environ.get('XDG_RUNTIME_DIR')),
    "wayland_display": str(os.environ.get('WAYLAND_DISPLAY')),
    "pulse_runtime_path": str(os.environ.get('PULSE_RUNTIME_PATH')),
    ...
}
session_defaults["waydroid_user_state"] = session_defaults["xdg_data_home"] + "/waydroid"
session_defaults["waydroid_data"] = session_defaults["waydroid_user_state"] + "/data"
```

你的用户名、uid、当前进程 pid、各种 XDG 路径、Wayland 和 PulseAudio 的位置，全在这。特别注意 `waydroid_data`——它指向 `~/.local/share/waydroid/data`，也就是说**安卓的用户数据是存在你家目录下的**，而不是 `/var/lib/waydroid` 那个系统目录。这设计是为了多用户隔离：每个 Linux 用户跑 Waydroid，各自的安卓数据互不干扰，都在自己家里。也正因如此，session 必须以普通用户身份跑——它得知道"你"是谁、你的家在哪，才能把数据放对地方。要是用 sudo 跑成了 root，这些路径全乱套，这又是前面那句"别用 sudo"警告背后的另一层原因。

`start()` 还会现场补两个动态值。一个是 DPI（第 73 到 80 行），它先读安卓属性 `ro.sf.lcd_density`，读不到就退而求其次用环境变量 `GRID_UNIT_PX` 乘以 20 估一个，再不行就给 0：

```python
dpi = tools.helpers.props.host_get(args, "ro.sf.lcd_density")
if dpi == "":
    dpi = os.getenv("GRID_UNIT_PX")
    if dpi is not None:
        dpi = str(int(dpi) * 20)
    else:
        dpi = "0"
session["lcd_density"] = dpi
```

DPI 直接影响安卓界面里字和图标的大小，得跟你的屏幕匹配上才不会糊或者过小。这个 fallback 链（属性 → 环境变量 → 0）也是 Waydroid 一贯的风格——拿不到精确值也要尽量估一个能用的，而不是直接摆烂报错。另一个补的值是 `background_start`，标记这次是不是后台启动，后面会讲它的用处。

这个 `session` 字典攒齐之后，等会儿会被原样塞进 DBus 调用，递给 root 那边的容器管理器——容器要靠它知道"这次会话是谁、Wayland 在哪、数据往哪放"。这也是为什么我说它是"把你是谁打包好"：一次会话的全部上下文，都浓缩在这一个字典里跨进程传递。两个进程一个 root 一个普通用户、地址空间完全隔离，没法共享内存里的对象，能传的就是这种可序列化的纯数据——DBus 的 `a{ss}`（字符串字典）签名也正是为此而设。

## DBus 双总线：两个进程怎么隔空喊话

> 图待补：DBus 双总线架构图：System Bus（Container/Initializer）与 Session Bus（SessionManager/Services）（Codex 超时/资源不足，请稍后用 image-generator 重出）

体检过了、身份打包好了，接下来 session 要请 root 那边把容器启动起来。这就用到了 Waydroid 的 DBus 双总线架构，我觉得这是整个项目里设计得最干净的一块，值得专门讲讲。

DBus 有两种总线。**系统总线（System Bus）**是全机器共享的、跟 root 服务打交道用的；**会话总线（Session Bus）**是每个登录用户私有的。Waydroid 把两个管理器恰好挂在两条总线上，对应它们各自的权限身份：

- 系统总线上，服务名 `id.waydro.Container`：`DbusContainerManager` 挂在 `/ContainerManager`，`DbusInitializer` 挂在 `/Initializer`。这俩都是 root 跑的。
- 会话总线上，服务名 `id.waydro.Session`：`DbusSessionManager` 挂在 `/SessionManager`，普通用户跑。

连接这两条总线的桥，是 `tools/helpers/ipc.py`，整个文件就 12 行，核心两个函数：

```python
def DBusContainerService(object_path="/ContainerManager", intf="id.waydro.ContainerManager"):
    return dbus.Interface(dbus.SystemBus().get_object("id.waydro.Container", object_path), intf)

def DBusSessionService(object_path="/SessionManager", intf="id.waydro.SessionManager"):
    return dbus.Interface(dbus.SessionBus().get_object("id.waydro.Session", object_path), intf)
```

`DBusContainerService()` 走系统总线找 root 的容器服务，`DBusSessionService()` 走会话总线找用户的 session 服务。短短两行，就是整个 Waydroid 跨进程调用的全部桥梁。

session 请求启动容器的那一下，就在 `start()` 第 98 行：

```python
try:
    tools.helpers.ipc.DBusContainerService().Start(session)
except dbus.DBusException as e:
    ...
    logging.error("WayDroid container is not listening")
    sys.exit(0)
```

一行 `DBusContainerService().Start(session)`,就把那个攒好的 `session` 字典隔着系统总线递给了 root 的容器管理器。容器那边收到后会先校验身份（确认发请求的确实是这个用户或 root），再走上一篇讲的 `do_start` 那一整套。如果容器服务压根没在跑，这里会捕获异常报"container is not listening"。

值得品一下这个分层的妙处：普通用户的 session 进程，没有权限自己去 `lxc-start`，但它可以隔着 DBus 礼貌地"请求" root 服务代劳，而 root 服务会严格校验请求者身份。权限该高的地方高、该低的地方低，中间用一道带鉴权的 DBus 缝合——这比"整个 Waydroid 都用 root 跑"安全太多了。

顺便看一眼容器那边 `Start` 方法的声明（`container_manager.py` 第 26 行），DBus 的方法签名信息量不小：

```python
@dbus.service.method("id.waydro.ContainerManager", in_signature='a{ss}',
                     out_signature='', sender_keyword="sender", connection_keyword="conn")
def Start(self, session, sender, conn):
    ...
    uid = dbus_info.GetConnectionUnixUser(sender)
    if str(uid) not in ["0", session["user_id"]]:
        raise RuntimeError("Cannot start a session on behalf of another user")
```

`in_signature='a{ss}'` 是 DBus 的类型签名,意思是"参数是一个字符串到字符串的字典"——这正好对应我们那个 `session` 字典。`sender_keyword="sender"` 让 DBus 框架把"是谁发的请求"也传进来，于是它能用 `GetConnectionUnixUser` 反查发起方的真实 uid，跟 `session` 里自称的 `user_id` 对一下。一个普通用户冒充另一个用户来启动容器？这道校验直接拦死。这种把"请求者身份"作为安全凭据来核验的做法，是 DBus 服务的标准范式，Waydroid 用得很到位。

`DbusSessionManager` 那边相反，对外接口极简，统共就一个 `Stop` 方法（第 22 行）——因为 session 大部分时候是"主动方"（发起请求、注册服务），真正需要被外部调用的就只有"停止"这一件事。

## Wayland socket 是怎么被"塞"进容器的

> 图待补：Wayland socket 绑定流程：宿主机 WAYLAND_DISPLAY → session LXC config → bind mount 到容器（Codex 超时/资源不足，请稍后用 image-generator 重出）

这里得插回去讲一个关键细节：容器是隔离的，那容器里的安卓到底怎么访问到宿主机的 Wayland socket？答案在上一篇提过的 `generate_session_lxc_config()`（`lxc.py` 第 183 到 217 行），它由容器管理器在 `do_start` 里调用，专门生成会话相关的挂载配置 `config_session`。

它要 bind mount 进容器的东西，全和你这次会话强相关。先在容器里建一个 tmpfs 当 `XDG_RUNTIME_DIR`，然后是重头戏——Wayland socket（第 199 到 202 行）：

```python
wayland_host_socket = os.path.realpath(os.path.join(
    session["xdg_runtime_dir"], session["wayland_display"]))
wayland_container_socket = os.path.realpath(os.path.join(
    tools.config.defaults["container_xdg_runtime_dir"],
    tools.config.defaults["container_wayland_display"]))
if not make_entry(wayland_host_socket, wayland_container_socket[1:]):
    raise OSError("Failed to bind Wayland socket")
```

它把宿主机上那个真实的 Wayland socket 文件（就是前面体检时确认存在的那个），bind mount 到容器内部的一个固定路径。这样一来，容器里的安卓往它以为是"自己的" Wayland socket 写数据，实际写到的是宿主合成器的 socket——画面就这么"流"到你桌面上了。这是个特别巧的招：不用任何网络转发、不用复制像素，就是文件系统层面把一个 socket 直接"借"进容器，安卓和你的桌面合成器其实在对着同一个 socket 说话。

紧接着还 bind 了 PulseAudio 的 socket（声音同理，安卓播放的声音直接走宿主的音频服务）和你的用户数据目录（rbind 成容器里的 `/data`）。

这里还有一道不能省的安全检查。看它内部的 `make_entry`（第 185 到 193 行）：

```python
def make_entry(src, dist=None, ...):
    if any(x in src for x in ["\n", "\r"]):
        logging.warning("User-provided mount path contains illegal character: " + src)
        return False
    if dist is None and (not os.path.exists(src) or
                         str(os.stat(src).st_uid) != session["user_id"]):
        logging.warning("User-provided mount path is not owned by user: " + src)
        return False
    return add_node_entry(...)
```

它会校验路径里没有换行符（防注入），而且要挂的东西必须是当前用户拥有的（`st_uid` 比对）。为什么这道检查不能省？因为这些路径来自用户会话、是用户那边传过来的，而执行 bind 的容器管理器是 root 权限。要是不校验，等于给了普通用户一个"让 root 帮我把任意文件挂进容器"的口子，那是个严重的提权漏洞。把这些写进 `config_session` 后，`lxc-start` 时它会和 `config_base`、`config_nodes` 一起被 LXC 读进去。

## 容器起来后，三个服务上场缝合桌面

> 图待补：跨系统服务架构：clipboard_manager(pyclip)、notification_manager、user_manager 的双向通信（Codex 超时/资源不足，请稍后用 image-generator 重出）

`DBusContainerService().Start(session)` 返回，意味着容器已经起来、安卓在开机了。控制权回到 `start()`，接下来是这一层的精华——启动三个"跨系统服务"，把容器和你的桌面真正缝到一起（第 107 到 110 行）：

```python
services.user_manager.start(args, session, unlocked_cb)
services.clipboard_manager.start(args)
services.notification_manager.start(args, session)
service(args, mainloop)
```

这三个服务有个共同的套路，我先讲清楚这个套路，三个就都好懂了：**它们都在一个 while 循环的线程里，通过 binder 往容器内的安卓注册一个服务，把一组 Python 回调函数交给安卓调用**。这是个反向的桥——前面讲的是 Python 隔着 DBus 调容器，这里反过来，是容器里的安卓通过 binder 回调到宿主的 Python 函数。双向打通，桌面集成才成立。

### clipboard_manager：剪贴板双向同步

最好懂的是剪贴板（`services/clipboard_manager.py`）。它依赖一个叫 `pyclip` 的库来读写宿主机的剪贴板，整个逻辑就两个回调：

```python
def sendClipboardData(value):
    try:
        pyclip.copy(value)        # 安卓 → 宿主：把安卓复制的内容写进宿主剪贴板
    except Exception as e:
        logging.debug(str(e))

def getClipboardData():
    try:
        return pyclip.paste()     # 宿主 → 安卓：把宿主剪贴板内容读给安卓
    except Exception as e:
        logging.debug(str(e))
    return ""

def service_thread():
    while not stopping:
        IClipboard.add_service(args, sendClipboardData, getClipboardData)
```

`IClipboard.add_service` 把这俩函数注册进容器里的安卓。你在安卓里 Ctrl+C，安卓就回调 `sendClipboardData` 把内容塞进宿主剪贴板；你在安卓里 Ctrl+V，安卓回调 `getClipboardData` 拿宿主剪贴板的内容。两个方向都通了，跨系统复制粘贴就这么实现的。

这里还有个优雅的降级：文件开头 `import pyclip` 是包在 try 里的，导入失败就把 `canClip` 设成 False，`start()` 里检测到没这个库，就跳过剪贴板服务、打个 debug 日志，整个 session 照常跑。少个剪贴板不至于让你用不了安卓——又是那个熟悉的"有则用、无则跳"的态度。

### notification_manager：把安卓通知转给桌面

通知管理器（`services/notification_manager.py`）思路类似，但方向主要是"安卓 → 桌面"。它启动时先连上宿主的标准通知服务（`org.freedesktop.Notifications`，这是 Linux 桌面通用的通知 DBus 接口）：

```python
dbus_proxy = dbus.Interface(dbus.SessionBus().get_object(
    "org.freedesktop.Notifications", "/org/freedesktop/Notifications"),
    "org.freedesktop.Notifications")
```

连不上（比如你的桌面没有通知服务）就直接跳过这个服务，照例不强求。连上了，就注册一个 `notify` 回调给安卓——安卓里某个 App 弹通知时，这个回调被触发，它把安卓通知的标题、正文、图标、紧急程度这些，翻译成 freedesktop 通知规范的格式，再 `dbus_proxy.Notify(...)` 转发给你的桌面通知中心。

它还处理了反向交互：你在桌面通知上点了某个按钮（action），通过 `onActionInvoked` 信号传回去，再通过 binder 通知容器里的安卓"用户点了这个动作"。连通知里的按钮点击都打通了，做得相当细。

### user_manager：把安卓 App 变成桌面图标

第三个 `user_manager`（`services/user_manager.py`）最有"存在感"——它就是让你能在桌面应用菜单里直接看到、点开安卓 App 的那个功臣。它注册的是 `IUserMonitor` 服务，核心回调是 `userUnlocked`（第 133 行）：

```python
def userUnlocked(uid):
    ...
    platformService = IPlatform.get_service(args)
    if platformService:
        appsList = platformService.getAppsInfo()
        for app in appsList:
            updateDesktopFile(app)       # 给每个安卓 App 生成 .desktop 文件
        for existing in apps_dir.glob("waydroid.*.desktop"):
            if existing.name not in (...):
                existing.unlink()         # 清理已卸载 App 的残留图标
    if unlocked_cb:
        unlocked_cb()
```

当安卓系统解锁就绪（用户数据可访问了），安卓回调 `userUnlocked`,它就去查容器里安装了哪些 App，给每个 App 在你的 `~/.local/share/applications` 下生成一个 `.desktop` 文件。这就是为什么你能在 GNOME/KDE 的应用列表里直接搜到"微信""支付宝"然后点开——本质上每个安卓 App 都被映射成了一个标准的 Linux 桌面快捷方式，点它就等于 `waydroid app launch <包名>`。

那个 `.desktop` 文件具体长啥样？看 `updateDesktopFile`（第 112 行起）就明白了：

```python
desktop_file.set_string("Desktop Entry", "Name", appInfo["name"])
desktop_file.set_string("Desktop Entry", "Exec", f"waydroid app launch {packageName}")
desktop_file.set_string("Desktop Entry", "Icon", str(waydroid_data_icons_dir / f"{packageName}.png"))
glib_key_file_prepend_string_list(desktop_file, "Desktop Entry", "Categories", ["X-WayDroid-App"])
...
desktop_file.set_string("Desktop Action app-settings", "Exec",
    f"waydroid app intent android.settings.APPLICATION_DETAILS_SETTINGS package:{packageName}")
```

`Name` 用安卓里的应用名，`Exec` 就是 `waydroid app launch 包名`，`Icon` 指向从安卓里抠出来的图标 png。它甚至还加了一个 `app-settings` 的桌面动作——你在图标上右键能直接跳到这个 App 的安卓设置页。细到这个程度，难怪用起来感觉跟原生 Linux 应用没两样。

它还有几处巧思值得一提。一是 `showApp` 判断（第 101 到 110 行）：只有带 `android.intent.category.LAUNCHER` 这个分类的 App 才生成图标——那些没有启动入口的后台服务类 App 就不该出现在应用菜单里。二是对系统自带 App（计算器、相机、设置那一串 `system_apps`）默认设 `NoDisplay=True`,免得你的应用菜单被一堆安卓内置应用塞爆。

它还注册了 `packageStateChanged` 回调，安卓里装了新 App 或卸了 App，桌面图标会跟着实时增删。注意那个 `unlocked_cb`——它是一路从 `start()` 传进来的回调，"安卓真正就绪"这个时刻就靠它通知上层。比如你 `waydroid app launch` 想启动某个 App，得等系统解锁了才能真正拉起，这个回调就是那个"绿灯"。

三个服务都启动后，`start()` 最后调 `service(args, mainloop)` 进入 GLib 主循环，session 进程就常驻在这儿,一边维持着 DBus 服务、一边让那三个服务线程持续工作。

### 为什么服务线程要套个死循环

你可能注意到了，三个服务的 `service_thread` 都长一个样：

```python
def service_thread():
    while not stopping:
        IClipboard.add_service(args, sendClipboardData, getClipboardData)
```

一个 `while not stopping` 死循环里反复调 `add_service`。第一次读我也纳闷——注册一次服务不就完了，干嘛循环？后来想明白：`add_service` 是个**阻塞调用**,它注册完服务后会一直跑着那个服务的事件循环，直到容器里的安卓端断开或重启。一旦安卓那头没了（比如容器重启），`add_service` 返回，外层 while 一看 `stopping` 还是 False，立马再注册一次。换句话说,这个循环是为了"容器重启后自动重连"——安卓那边一恢复，服务马上重新挂上去，剪贴板、通知、图标这些功能不用你手动重启 session 就能自己续上。停止时把 `stopping` 设成 True，循环自然就退出来了。一个朴素的 while,扛起了"断线重连"的活。

### session 跟着桌面一起退场

`service()` 函数（第 31 到 38 行）还藏了一个体贴的细节：

```python
def service(args, looper):
    bus = dbus.SessionBus()
    bus.set_exit_on_disconnect(False)
    bus.add_signal_receiver(lambda: handle_disconnect(args, looper),
                            signal_name='Disconnected',
                            dbus_interface='org.freedesktop.DBus.Local')
    ...
```

它监听会话总线的 `Disconnected` 信号。这个信号什么时候来？当你的桌面会话总线没了——通常就是你**注销登录或关机**的时候。一旦收到，`handle_disconnect` 就被触发，把 session 自己停掉、连容器也一起停。这意味着你退出登录时，Waydroid 会自动跟着干净退场，不会留一个孤儿容器在后台空跑。`set_exit_on_disconnect(False)` 则是先关掉 DBus 库默认的"断连即退出"行为，好让 Waydroid 自己用上面那套逻辑优雅地收尾，而不是被库粗暴地直接杀掉。从你主动 Ctrl+C，到注销登录，到容器那边发 SIGUSR1，Waydroid 把所有"该退场"的场景都接住了——这种善始善终，是个成熟工具该有的样子。

## 那 App 到底是哪一步"显示"出来的

讲到这儿你可能还有个疑问：服务都缝好了，但安卓的画面/App 是哪一步真正弹到屏幕上的？这要回到前面那个 `background_start` 和 `unlocked_cb`。

`start()` 有个 `background` 参数（第 40 行），默认 True。`waydroid session start` 走的是后台启动——它只把系统和服务拉起来，并不主动弹任何窗口，安卓在后台待命。真正让界面出现的是另外两条命令：`waydroid show-full-ui` 把整个安卓桌面铺出来，`waydroid app launch <包名>` 启动单个 App。这俩最终都通过 binder 调容器里的 `IPlatform` 服务，把对应画面渲染到我们前面 bind 进去的那个 Wayland socket 上——画面就贴到你桌面了。

但这里有个时序坑：你刚 session start 完，安卓可能还在开机、还没解锁，这时候你就 `app launch` 是会扑空的。`unlocked_cb` 就是来解决这个的。还记得 `user_manager` 里 `userUnlocked` 回调最后那句 `if unlocked_cb: unlocked_cb()` 吗？它就是在安卓真正解锁就绪的那一刻,回调通知上层"现在可以动手了"。所以像安装完 App 想立刻启动这种场景，代码会用 `background=False` 加一个 `launchNow` 回调一起传进 `start()`，等 `unlocked_cb` 这盏绿灯亮了再真正拉起 App。整个"等系统就绪"的异步协调，就靠这一个回调串起来,既不会傻等死循环,也不会过早扑空。

## 怎么收场：停止与信号处理

有始得有终。session 的停止逻辑在 `do_stop()` 和 `stop()`（第 112 到 128 行）：

```python
def do_stop(args, looper):
    services.user_manager.stop(args)
    services.clipboard_manager.stop(args)
    services.notification_manager.stop(args)
    looper.quit()

def stop(args):
    try:
        tools.helpers.ipc.DBusSessionService().Stop()
    except dbus.DBusException:
        stop_container(quit_session=True)
```

`do_stop` 很直白：把三个服务挨个停掉（每个服务的 `stop` 就是把 `stopping` 标志位设 True 让 while 循环退出，再 quit 各自的 loop），然后退出主循环。而 `stop` 是给外部命令用的——你敲 `waydroid session stop`,它通过会话总线调 `DBusSessionService().Stop()` 通知正在跑的 session 进程收摊；要是连 session 服务都没在跑，就退而直接去停容器。

最精彩的是信号处理（第 86 到 96 行），它把 Unix 信号和这套停止逻辑接了起来：

```python
def sigint_handler(data):
    do_stop(args, mainloop)
    stop_container(quit_session=False)

def sigusr_handler(data):
    do_stop(args, mainloop)

GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGHUP, sigint_handler, None)
GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, sigint_handler, None)
GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, sigint_handler, None)
GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, sigusr_handler, None)
```

`SIGHUP`/`SIGINT`/`SIGTERM`（你 Ctrl+C、或者关掉终端、或者系统要关机）都走 `sigint_handler`：先停自己的服务，再顺手把容器也停掉。这合理——session 都没了，留着容器空跑没意义。

但 `SIGUSR1` 单独走 `sigusr_handler`,它只 `do_stop` 停自己、**不去碰容器**。为什么要区别对待？回想上一篇——容器管理器 `stop` 容器的时候，会主动给 session 进程发一个 `SIGUSR1`。这时候容器**正在被停**，session 收到信号要做的是"我自己收摊就行，千万别反过来再去停容器"，否则就成了 session 停容器、容器又通知 session、两边互相调用的死结了。用一个专门的信号把"我主动停"和"容器通知我停"这两种场景区分开，避免循环——这种对协作边界的精细处理，是读到这儿我最佩服的一笔。

## 串起来看这条链

把这一篇连起来：你敲 `waydroid session start`，session 进程以普通用户身份起来，先抢会话总线名字（保证单例），再体检 Wayland/XDG 环境（不行就拦下并提醒别用 sudo），把你的身份和环境打包成 `session` 字典；然后隔着 DBus 系统总线请求 root 的容器管理器 `Start(session)`，容器那边校验身份后挂好 Wayland socket、启动安卓；容器起来后，session 启动 clipboard / notification / user 三个服务，通过 binder 反向注册回调，把剪贴板、通知、App 图标统统缝进你的桌面；最后进主循环常驻，靠精心设计的信号处理优雅收场。

我越读越觉得，Waydroid 这套 session 设计的核心智慧就俩字——"边界"。权限的边界（root 容器 vs 用户 session）、进程的边界（两条 DBus 总线分得清清楚楚）、协作的边界（用 SIGUSR1 区分"主动停"和"被通知停"）。安卓是个完整又霸道的系统，要把它装进 Linux 桌面又不让它越界、还得让该通的体验通起来，靠的不是什么黑魔法，而是每一处边界都想清楚了"谁该干什么、谁不该干什么"。

想自己读代码的话，建议从 `session_manager.py` 的 `start()` 进去当主线，遇到 `DBusContainerService().Start` 就跳去看上一篇讲的容器启动，遇到三个 `services.xxx.start` 就分别翻 `services/` 下那三个文件——每个都不长，套路又一致，看一个就会看三个。把 session 这层吃透，"安卓画面怎么上的桌面、剪贴板怎么通的"这些日常你天天在用却没细想过的事，就全说得清了。


---

本文由 AgentPlanFlow 生成
