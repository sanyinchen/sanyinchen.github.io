---
title: 一、环境准备
author: sanyinchen
date: 2025-06-19 16:00:00 +0800
categories: [操作系统, Android2.3系统源码]
tags: [Android,System]
render_with_liquid: false
---

#### 一、下载Android 2.3系统源码（以下相关下载仓库均使用国内镜像）
+ 下载repo： curl https://mirrors.tuna.tsinghua.edu.cn/git/git-repo -o ~/bin/repo
  + chmod a+x ~/bin/repo
  + 修改REPO_URL为https://mirrors.tuna.tsinghua.edu.cn/git/git-repo
+ repo init -u https://aosp.tuna.tsinghua.edu.cn/platform/manifest -b android-2.3.5_r1
  + repo sync -j 4

#### 二、下载docker镜像
+ sudo docker pull docker.1panel.live/ak1pe/android-aosp-build:android-2.3
  + `docker.1panel.live` 失效可替换为其他国内镜像源

#### 三、编译测试
+ 启动docker容器
  + sudo docker run -d -v $REPO_SOURCE_PATH:/workspace -e UID=$UID -e GID=$UID --name builder-android-2.3 docker.1panel.live/ak1pe/android-aosp-build:android-2.3 ($REPO_SOURCE_PATH替换为源码目录,docker.1panel.live为二中的仓库镜像地址)
  + sudo docker exec -it builder-android-2.3 bash
+ 修改python为python2.7
  + mv -f /usr/bin/python2.7 /usr/bin/python
+ 初始化环境
  + source build/envsetup.sh
  + lunch
  + 1
+ 编译
  + make -j4
+ 已知编译问题：
  + frameworks/base/libs/utils/RefBase.cpp:507:67: error: passing ‘const android::RefBase::weakref_impl’ as ‘this’ argument of ‘void android::RefBase::weakref_impl::trackMe(bool, bool)’ discards qualifiers [-fpermissive]
    + vim frameworks/base/libs/utils/Android.mk
    + `LOCAL_CFLAGS += -DLIBUTILS_NATIVE=1 $(TOOL_CFLAGS)` 修改为 `LOCAL_CFLAGS += -DLIBUTILS_NATIVE=1 $(TOOL_CFLAGS) -fpermissive`
  + error: “_FORTIFY_SOURCE” redefined [-Werror]
    + vim build/core/combo/HOST_linux-x86.mk
    + `HOST_GLOBAL_CFLAGS += -D_FORTIFY_SOURCE=0`修改为`HOST_GLOBAL_CFLAGS += -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0`
+ 其他
  + windows wsl2 挂载`docker run -d -v \\wsl.localhost\Ubuntu\home\source\2.3:/workspace -e  CASE_SENSITIVE=1 -e UID=$UID -e GID=$UID --privileged=true --name builder-android-2.3 docker.1panel.live/ak1pe/android-aosp-build:android-2.3`
  + 重建docker 容器：
    + docker stop builder-android-2.3
    + docker rm builder-android-2.3
