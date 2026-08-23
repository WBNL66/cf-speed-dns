#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cloudflare DNS 智能优选 IP

功能：
1. 获取当前 Cloudflare DNS A 记录
2. 获取 ipTop.html 优选 IP
3. 对当前 IP 和候选 IP 进行真实下载测速
4. 候选 IP 至少比当前 IP 快 10% 才更换
5. 没有更快的 IP 则保持不变
6. 测速失败不会更换 IP
7. Telegram 推送检测结果
"""

import os
import re
import time
import statistics
import traceback

import requests
import urllib3

# 忽略 verify=False 的 HTTPS 警告
urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)


# =========================================================
# 环境变量
# =========================================================

CF_API_TOKEN = os.environ.get("CF_API_TOKEN")
CF_ZONE_ID = os.environ.get("CF_ZONE_ID")
CF_DNS_NAME = os.environ.get("CF_DNS_NAME")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")


# =========================================================
# 参数
# =========================================================

DEFAULT_TIMEOUT = 20

# 候选 IP 至少比当前 IP 快多少才更换
# 0.10 = 10%
MIN_SPEED_IMPROVEMENT = 0.10

# 每个 IP 测试次数
TEST_ROUNDS = 2

# 每次测速最大下载字节数
# 5MB
TEST_BYTES = 5 * 1024 * 1024

# 最多测试多少个候选 IP
MAX_CANDIDATES = 5

# 测速失败重试次数
SPEED_RETRIES = 2


# =========================================================
# Cloudflare 请求头
# =========================================================

CF_HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "Cloudflare-IP-Optimizer/2.0"
}


# =========================================================
# Telegram
# =========================================================

def send_telegram(message):
    """发送 Telegram 通知"""

    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(
            "TG_BOT_TOKEN 或 TG_CHAT_ID 未设置，"
            "跳过 Telegram 推送"
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TG_BOT_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        response = requests.post(
            url,
            json=data,
            timeout=DEFAULT_TIMEOUT
        )

        if response.status_code == 200:
            print("Telegram 推送成功")
            return True

        print(
            f"Telegram 推送失败："
            f"{response.status_code} "
            f"{response.text}"
        )

    except Exception as e:
        print(f"Telegram 推送异常：{e}")

    return False


# =========================================================
# 获取 Cloudflare DNS 记录
# =========================================================

def get_dns_records():
    """获取指定域名的 A 记录"""

    url = (
        f"https://api.cloudflare.com/client/v4/"
        f"zones/{CF_ZONE_ID}/dns_records"
    )

    params = {
        "type": "A",
        "name": CF_DNS_NAME,
        "per_page": 100
    }

    try:
        response = requests.get(
            url,
            headers=CF_HEADERS,
            params=params,
            timeout=DEFAULT_TIMEOUT
        )

        if response.status_code != 200:
            print(
                f"获取 Cloudflare DNS 失败："
                f"{response.status_code}"
            )
            print(response.text)
            return []

        result = response.json()

        if not result.get("success"):
            print(
                "Cloudflare API 返回失败：",
                result
            )
            return []

        records = []

        for record in result.get("result", []):

            if (
                record.get("type") == "A"
                and record.get("name") == CF_DNS_NAME
            ):

                records.append(record)

        return records

    except Exception as e:

        print(f"获取 DNS 记录异常：{e}")
        traceback.print_exc()

        return []


# =========================================================
# 获取优选 IP
# =========================================================

def get_candidate_ips():
    """
    获取 ipTop.html 中的 IPv4 地址
    """

    url = "https://ip.164746.xyz/ipTop.html"

    try:

        response = requests.get(
            url,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "User-Agent":
                "Mozilla/5.0 "
                "Cloudflare-IP-Optimizer"
            }
        )

        if response.status_code != 200:

            print(
                f"获取优选 IP 失败："
                f"HTTP {response.status_code}"
            )

            return []

        # 提取 IPv4
        ips = re.findall(
            r"\b"
            r"(?:25[0-5]|2[0-4]\d|"
            r"1\d\d|[1-9]?\d)"
            r"(?:\."
            r"(?:25[0-5]|2[0-4]\d|"
            r"1\d\d|[1-9]?\d)){3}"
            r"\b",
            response.text
        )

        # 去重
        unique_ips = []

        for ip in ips:

            if ip not in unique_ips:
                unique_ips.append(ip)

        print(
            f"获取到优选 IP："
            f"{unique_ips}"
        )

        return unique_ips[:MAX_CANDIDATES]

    except Exception as e:

        print(
            f"获取优选 IP 异常：{e}"
        )

        traceback.print_exc()

        return []


# =========================================================
# 单次真实测速
# =========================================================

def single_speed_test(ip):
    """
    对 Cloudflare IP 进行真实 HTTP 下载测速。

    使用 Cloudflare speed test 下载接口：
    /__down?bytes=xxxxxxx

    通过 Host 指定 speed.cloudflare.com。
    """

    url = (
        f"https://{ip}/__down"
        f"?bytes={TEST_BYTES}"
    )

    headers = {
        "Host": "speed.cloudflare.com",
        "User-Agent":
            "Mozilla/5.0 "
            "Cloudflare-IP-Optimizer",
        "Accept": "*/*",
        "Connection": "close"
    }

    try:

        start_time = time.perf_counter()

        response = requests.get(
            url,
            headers=headers,
            timeout=15,
            verify=False,
            stream=True
        )

        if response.status_code != 200:

            print(
                f"{ip} HTTP 状态码："
                f"{response.status_code}"
            )

            response.close()
            return 0.0

        total_bytes = 0

        for chunk in response.iter_content(
            chunk_size=64 * 1024
        ):

            if not chunk:
                continue

            total_bytes += len(chunk)

            if total_bytes >= TEST_BYTES:
                break

        response.close()

        elapsed = (
            time.perf_counter()
            - start_time
        )

        if elapsed <= 0 or total_bytes <= 0:
            return 0.0

        speed_mbps = (
            total_bytes * 8
            / elapsed
            / 1_000_000
        )

        return round(speed_mbps, 2)

    except Exception as e:

        print(
            f"{ip} 单次测速失败：{e}"
        )

        return 0.0


# =========================================================
# 多次测速
# =========================================================

def test_ip_speed(ip):
    """
    多次测速，取中位数。
    """

    results = []

    print(
        f"\n开始测速：{ip}"
    )

    for round_no in range(TEST_ROUNDS):

        speed = 0.0

        for retry in range(SPEED_RETRIES):

            speed = single_speed_test(ip)

            if speed > 0:
                break

            print(
                f"{ip} 第 {round_no + 1} 次测速失败，"
                f"重试 {retry + 1}/{SPEED_RETRIES}"
            )

        if speed > 0:

            results.append(speed)

            print(
                f"{ip} 第 "
                f"{round_no + 1}/{TEST_ROUNDS} 次："
                f"{speed} Mbps"
            )

        else:

            print(
                f"{ip} 第 "
                f"{round_no + 1}/{TEST_ROUNDS}：失败"
            )

    if not results:

        print(
            f"{ip}：测速全部失败"
        )

        return 0.0

    # 中位数比单次最高速度更稳定
    median_speed = statistics.median(
        results
    )

    median_speed = round(
        median_speed,
        2
    )

    print(
        f"{ip} 最终测速："
        f"{median_speed} Mbps"
    )

    return median_speed


# =========================================================
# 更新 Cloudflare DNS
# =========================================================

def update_dns_record(
    record,
    new_ip
):
    """
    更新 DNS，同时尽量保留原记录属性。
    """

    record_id = record["id"]
    old_ip = record.get("content", "")

    if old_ip == new_ip:

        print(
            "新旧 IP 相同，不更新。"
        )

        return True

    url = (
        f"https://api.cloudflare.com/client/v4/"
        f"zones/{CF_ZONE_ID}/dns_records/"
        f"{record_id}"
    )

    # 尽量保留原记录设置
    data = {
        "type": "A",
        "name": record.get(
            "name",
            CF_DNS_NAME
        ),
        "content": new_ip,
        "ttl": record.get(
            "ttl",
            1
        ),
        "proxied": record.get(
            "proxied",
            False
        )
    }

    # 如果原记录有 comment / tags，则保留
    if "comment" in record:
        data["comment"] = record["comment"]

    if "tags" in record:
        data["tags"] = record["tags"]

    try:

        response = requests.put(
            url,
            headers=CF_HEADERS,
            json=data,
            timeout=DEFAULT_TIMEOUT
        )

        result = response.json()

        if (
            response.status_code == 200
            and result.get("success")
        ):

            print(
                f"DNS 更新成功："
                f"{old_ip} -> {new_ip}"
            )

            return True

        print(
            "DNS 更新失败："
        )

        print(response.text)

    except Exception as e:

        print(
            f"DNS 更新异常：{e}"
        )

        traceback.print_exc()

    return False


# =========================================================
# 主程序
# =========================================================

def main():

    print("=" * 60)
    print("Cloudflare DNS 智能优选 IP")
    print("=" * 60)

    # -----------------------------------------------------
    # 检查环境变量
    # -----------------------------------------------------

    if not CF_API_TOKEN:
        print("错误：CF_API_TOKEN 未设置")
        return

    if not CF_ZONE_ID:
        print("错误：CF_ZONE_ID 未设置")
        return

    if not CF_DNS_NAME:
        print("错误：CF_DNS_NAME 未设置")
        return

    # -----------------------------------------------------
    # 获取当前 DNS
    # -----------------------------------------------------

    records = get_dns_records()

    if not records:

        print(
            f"错误：没有找到 "
            f"{CF_DNS_NAME} 的 A 记录"
        )

        return

    # 只处理第一个 A 记录
    record = records[0]

    current_ip = record.get(
        "content",
        ""
    )

    if not current_ip:

        print(
            "错误：当前 DNS IP 为空"
        )

        return

    print(
        f"\n当前 DNS："
        f"{CF_DNS_NAME}"
    )

    print(
        f"当前 IP：{current_ip}"
    )

    # -----------------------------------------------------
    # 测试当前 IP
    # -----------------------------------------------------

    current_speed = test_ip_speed(
        current_ip
    )

    if current_speed <= 0:

        print(
            "当前 IP 测速失败。"
            "为防止误换 IP，本次不进行更换。"
        )

        send_telegram(
            "⚠️ <b>Cloudflare IP 检测</b>\n\n"
            f"域名：<code>{CF_DNS_NAME}</code>\n"
            f"当前 IP：<code>{current_ip}</code>\n\n"
            "❌ 当前 IP 测速失败\n"
            "⏸️ 为防止误换 IP，本次保持不变。"
        )

        return

    # -----------------------------------------------------
    # 获取候选 IP
    # -----------------------------------------------------

    candidates = get_candidate_ips()

    if not candidates:

        print(
            "没有获取到候选 IP，保持当前 IP。"
        )

        send_telegram(
            "⚠️ <b>Cloudflare IP 检测</b>\n\n"
            f"当前 IP：<code>{current_ip}</code>\n"
            f"当前速度：<b>{current_speed} Mbps</b>\n\n"
            "❌ 未获取到候选优选 IP\n"
            "⏸️ 保持当前解析。"
        )

        return

    # -----------------------------------------------------
    # 删除当前 IP
    # -----------------------------------------------------

    candidates = [
        ip
        for ip in candidates
        if ip != current_ip
    ]

    if not candidates:

        print(
            "候选 IP 与当前 IP 相同，"
            "无需更换。"
        )

        send_telegram(
            "ℹ️ <b>Cloudflare IP 检测</b>\n\n"
            f"当前 IP：<code>{current_ip}</code>\n"
            f"当前速度：<b>{current_speed} Mbps</b>\n\n"
            "没有新的候选 IP。\n"
            "⏸️ 保持当前解析。"
        )

        return

    # -----------------------------------------------------
    # 测试候选 IP
    # -----------------------------------------------------

    speed_results = []

    for ip in candidates:

        speed = test_ip_speed(ip)

        if speed > 0:

            speed_results.append(
                (ip, speed)
            )

    if not speed_results:

        print(
            "所有候选 IP 测速失败，"
            "保持当前 IP。"
        )

        send_telegram(
            "⚠️ <b>Cloudflare IP 检测</b>\n\n"
            f"当前 IP：<code>{current_ip}</code>\n"
            f"当前速度：<b>{current_speed} Mbps</b>\n\n"
            "❌ 所有候选 IP 测速失败\n"
            "⏸️ 保持当前解析。"
        )

        return

    # -----------------------------------------------------
    # 找最快候选
    # -----------------------------------------------------

    speed_results.sort(
        key=lambda x: x[1],
        reverse=True
    )

    best_ip, best_speed = (
        speed_results[0]
    )

    # -----------------------------------------------------
    # 计算提升比例
    # -----------------------------------------------------

    improvement = (
        best_speed - current_speed
    ) / current_speed

    improvement_percent = round(
        improvement * 100,
        2
    )

    print("\n" + "=" * 60)

    print(
        f"当前 IP：{current_ip}"
    )

    print(
        f"当前速度："
        f"{current_speed} Mbps"
    )

    print(
        f"最快候选：{best_ip}"
    )

    print(
        f"候选速度："
        f"{best_speed} Mbps"
    )

    print(
        f"速度提升："
        f"{improvement_percent}%"
    )

    print("=" * 60)

    # -----------------------------------------------------
    # 判断是否换 IP
    # -----------------------------------------------------

    should_change = (
        best_speed > current_speed
        and improvement >= MIN_SPEED_IMPROVEMENT
    )

    # -----------------------------------------------------
    # 更快 → 更新 DNS
    # -----------------------------------------------------

    if should_change:

        print(
            "\n发现更快 IP，"
            "准备更新 Cloudflare DNS。"
        )

        success = update_dns_record(
            record,
            best_ip
        )

        if success:

            message = (
                "🚀 <b>Cloudflare IP 已优化</b>\n\n"
                f"域名：<code>{CF_DNS_NAME}</code>\n\n"
                f"旧 IP：<code>{current_ip}</code>\n"
                f"旧速度：<b>{current_speed} Mbps</b>\n\n"
                f"新 IP：<code>{best_ip}</code>\n"
                f"新速度：<b>{best_speed} Mbps</b>\n\n"
                f"📈 提升：<b>{improvement_percent}%</b>\n"
                "✅ 已自动更换"
            )

        else:

            message = (
                "❌ <b>Cloudflare DNS 更新失败</b>\n\n"
                f"当前 IP：<code>{current_ip}</code>\n"
                f"当前速度：<b>{current_speed} Mbps</b>\n\n"
                f"候选 IP：<code>{best_ip}</code>\n"
                f"候选速度：<b>{best_speed} Mbps</b>\n\n"
                "DNS 保持原 IP。"
            )

    # -----------------------------------------------------
    # 没有明显更快 → 不换
    # -----------------------------------------------------

    else:

        reason = ""

        if best_speed <= current_speed:

            reason = (
                "最佳候选没有当前 IP 快。"
            )

        else:

            reason = (
                f"虽然候选更快，但只提升 "
                f"{improvement_percent}%，"
                f"不足 {MIN_SPEED_IMPROVEMENT * 100:.0f}% "
                "的更换阈值。"
            )

        message = (
            "⏸️ <b>Cloudflare IP 保持不变</b>\n\n"
            f"域名：<code>{CF_DNS_NAME}</code>\n\n"
            f"当前 IP：<code>{current_ip}</code>\n"
            f"当前速度：<b>{current_speed} Mbps</b>\n\n"
            f"最佳候选：<code>{best_ip}</code>\n"
            f"候选速度：<b>{best_speed} Mbps</b>\n\n"
            f"速度差：<b>{improvement_percent}%</b>\n\n"
            f"📌 {reason}\n"
            "⏸️ 本次不更换。"
        )

    # -----------------------------------------------------
    # Telegram
    # -----------------------------------------------------

    send_telegram(message)

    print("\n任务完成。")


# =========================================================
# 入口
# =========================================================
