#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import json
import requests
from bs4 import BeautifulSoup

POST_URL = "https://leetcode.cn/discuss/post/3581838/fen-xiang-gun-ti-dan-dong-tai-gui-hua-ru-007o/"
API_ALL_PROBLEMS = "https://leetcode.cn/api/problems/all/"
OUT_MD = "dp_题单_表格版.md"

# 你的博客头
FRONT_MATTER = """---
title: 一、环境准备
author: sanyinchen
date: 2025-06-19 16:00:00 +0800
categories: [操作系统, Android2.3系统源码]
tags: [Android,System]
render_with_liquid: false
toc: true
---

# DP算法清单

题单来源：LeetCode 讨论贴《分享丨〖算法题单〗动态规划（入门/背包/划分/状态机/区间/状压/数位/树形/优化）》
原帖：{post_url}

# 刷题状态说明

| 标记 | 含义 |
|---|---|
| ❌ | 未做 |
| 🟡 | 已做但没完全理解 |
| ✅ | 已掌握 |
| ⭐ | 经典题 |

""".format(post_url=POST_URL)


def fetch_post_text(url: str) -> str:
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # 讨论贴正文一般在 markdown 渲染区域；这里用“尽量多取文本”的策略
    # 如果后续 LeetCode 页面结构改了，你可以 print(soup.prettify()[:2000]) 看一下
    text = soup.get_text("\n")
    return text


def fetch_id_to_slug() -> dict[int, str]:
    r = requests.get(API_ALL_PROBLEMS, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()

    # data["stat_status_pairs"] 里包含题号、slug 等
    mp: dict[int, str] = {}
    for it in data.get("stat_status_pairs", []):
        stat = it.get("stat") or {}
        fid = stat.get("frontend_question_id")
        slug = stat.get("question__title_slug")
        if isinstance(fid, int) and isinstance(slug, str):
            mp[fid] = slug
    return mp


# 解析帖子里的“章节标题 + 题目行”
# 题目行形如： "  * 70. 爬楼梯" 或 "  * LCP 47. 入场安检" 或 "  * 面试题 17.06. 2 出现的次数"
# 我们尽量抽取“题号(含前缀) + 标题”
PROBLEM_LINE = re.compile(
    r"^\s*[*•\-]\s*(?P<id>(?:LCP|LCR|面试题|剑指 Offer|Offer|LC)\s*\d+(?:\.\d+)?|\d+)\.\s*(?P<title>.+?)\s*$"
)

# 章节标题行，例如 "## 一、入门 DP" / "### §1.1 爬楼梯"
HEADER_LINE = re.compile(r"^(?P<h>#{2,4})\s+(?P<title>.+?)\s*$")


def normalize_title(raw: str) -> str:
    # 去掉难度分、括号提示等尾部信息（尽量不影响原名）
    # 例如： "746. 使用最小花费爬楼梯 约 1500" -> "使用最小花费爬楼梯"
    s = raw.strip()
    # 常见尾部：数字分、"（会员题）"、"同 XXX 题" 等，保守一点只截掉明显“评分数字”
    s = re.sub(r"\s+\d{3,4}\s*$", "", s)
    s = re.sub(r"\s+约\s*\d{3,4}\s*$", "", s)
    return s.strip()


def make_link(pid: str, id2slug: dict[int, str]) -> str:
    # 纯数字题号：用 slug
    if pid.isdigit():
        n = int(pid)
        slug = id2slug.get(n)
        if slug:
            return f"https://leetcode.cn/problems/{slug}/"
        # 兜底：搜索页（至少可点）
        return f"https://leetcode.cn/problemset/all/?search={n}"

    # 其它前缀（LCP/LCR/面试题等）：slug 不一定稳定，仍然用搜索兜底
    # 如果 API 里也能找到对应前端号，你可以自行扩展映射逻辑
    q = pid.replace(" ", "")
    return f"https://leetcode.cn/problemset/all/?search={q}"


def main():
    post_text = fetch_post_text(POST_URL)
    id2slug = fetch_id_to_slug()

    lines = post_text.splitlines()

    out = []
    out.append(FRONT_MATTER)

    current_headers = []

    # 用于去重（同一题可能在“扩展/优化/专题”重复出现）
    seen = set()

    def flush_headers():
        # 把最新的 headers 输出成 markdown 标题
        for level, title in current_headers:
            out.append(f"{'#' * level} {title}\n")

    for ln in lines:
        m_h = HEADER_LINE.match(ln)
        if m_h:
            h = m_h.group("h")
            title = m_h.group("title").strip()
            level = len(h)
            # 只保留 2~4 级标题（和原帖结构接近）
            if 2 <= level <= 4:
                current_headers = [(level, title)]
                out.append(f"\n{'#' * level} {title}\n")
            continue

        m = PROBLEM_LINE.match(ln)
        if not m:
            continue

        pid = m.group("id").strip()
        title = normalize_title(m.group("title"))

        key = (pid, title)
        if key in seen:
            continue
        seen.add(key)

        link = make_link(pid if pid.isdigit() else pid, id2slug)
        # 每遇到一题，如果上一个不是表格头，就补表头
        if not out or not out[-1].startswith("| 题号"):
            out.append("| 题号 | 题目 | 链接 | 是否完成 | 备注 |\n")
            out.append("|---|---|---|---|---|\n")

        # 链接列：用 slug 生成的 URL（或搜索兜底），同时保留可读性
        out.append(f"| {pid} | {title} | [Link]({link}) | ❌ | |\n")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.writelines(out)

    print(f"[OK] 已生成：{OUT_MD} （共 {len(seen)} 题）")


if __name__ == "__main__":
    main()
