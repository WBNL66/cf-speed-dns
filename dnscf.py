#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cloudflare DNS 智能优选 IP

功能：
1. 获取当前 Cloudflare DNS A 记录
2. 获取 ipTop.html 优选 IP
3. 使用 curl --resolve 进行真实 HTTPS 下载测速
4. 正确使用 speed.cloudflare.com 的 TLS SNI
5. 每个 IP 测速 2 次，取中位数
6. 候选 IP 至少比当前 IP 快 10% 才更换
7. 当前 IP 测速失败不会误换 IP
8. 无论换不换 IP 都发送 Telegram 通知
9. DNS 更新失败会发送 Telegram 通知
10. 保留 Cloudflare 原有记录属性
"""

import os
import re
import time
import statistics
import subprocess
import traceback

import requests


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

# Cloudflare / Telegram API 超时时间
DEFAULT_TIMEOUT = 20

# 候选 IP 至少比当前 IP 快 10% 才更换
MIN_SPEED_IMPROVEMENT = 0.10

# 每个 IP 测速次数
TEST_ROUNDS = 2

# 每次下载测试 5 MB
TEST_BYTES = 5 * 1024 * 1024

# 最多测试多少个候选 IP
MAX_CANDIDATES = 5

# 单次测速超时时间
SPEED_TIMEOUT = 20

# 测速失败后的重试次数
SPEED_RETRIES = 2

# Cloudflare 官方测速域名
SPEED_HOST = "speed.cloudflare.com"


# =========================================================
# Cloudflare API Headers
# =========================================================

CF_HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "Cloudflare-IP-Optimizer/4.0"
}


# =========================================================
# Telegram
# =========================================================

def send_telegram(message):
    """
    发送 Telegram 通知
    """

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
            f"{response.status_code}"
        )

        print(response.text)

    except Exception as e:

        print(
            f"Telegram 推送异常：{e}"
        )

    return False


# =========================================================
# 获取 Cloudflare DNS
# =========================================================

def get_dns_records():
    """
    获取指定域名的 A 记录
    """

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
                "Cloudflare API 返回失败："
            )

            print(result)

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

        print(
            f"获取 DNS 记录异常：{e}"
        )

        traceback.print_exc()

        return []


# =========================================================
# 获取优选 IP
# =========================================================

def get_candidate_ips():
    """
    从 ipTop.html 提取 IPv4
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

        # IPv4 正则
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
# 检查 curl
# =========================================================

def check_curl():
    """
    检查 GitHub Actions 环境是否存在 curl。
    Ubuntu runner 默认自带 curl。
    """

    try:

        result = subprocess.run(
            ["curl", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10
        )

        if result.returncode == 0:

            print(
                "curl 检测成功："
                + result.stdout.splitlines()[0]
            )

            return True

    except Exception as e:

        print(
            f"检测 curl 失败：{e}"
        )

    return False


# =========================================================
# 单次测速
# =========================================================

def single_speed_test(ip):
    """
    使用 curl --resolve 测速。

    关键：

    --resolve speed.cloudflare.com:443:IP

    这样：

    DNS：
        speed.cloudflare.com -> 指定 IP

    TLS SNI：
        speed.cloudflare.com

    实际连接：
        指定 IP

    从而避免：

        SSLV3_ALERT_HANDSHAKE_FAILURE
    """

    url = (
        f"https://{SPEED_HOST}/__down"
        f"?bytes={TEST_BYTES}"
    )

    resolve = (
        f"{SPEED_HOST}:443:{ip}"
    )

    command = [
        "curl",

        "--silent",
        "--show-error",

        "--location",

        "--http1.1",

        "--connect-timeout",
        "8",

        "--max-time",
        str(SPEED_TIMEOUT),

        "--resolve",
        resolve,

        "--output",
        "/dev/null",

        "--write-out",
        "%{http_code}|%{speed_download}|%{time_total}",

        url
    ]

    try:

        print(
            f"{ip} 开始测速..."
        )

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SPEED_TIMEOUT + 5
        )

        output = result.stdout.strip()

        error = result.stderr.strip()

        if result.returncode != 0:

            print(
                f"{ip} curl 失败："
                f"{error}"
            )

            return 0.0

        # =================================================
        # 解析 curl 输出
        #
        # HTTP状态码 | 下载速度(bytes/s) | 总时间
        # =================================================

        parts = output.split("|")

        if len(parts) != 3:

            print(
                f"{ip} 无法解析测速结果："
                f"{output}"
            )

            return 0.0

        http_code = parts[0]

        speed_bytes = float(
            parts[1]
        )

        total_time = float(
            parts[2]
        )

        # =================================================
        # HTTP 状态
        # =================================================

        if http_code != "200":

            print(
                f"{ip} HTTP 状态码："
                f"{http_code}"
            )

            return 0.0

        # =================================================
        # 下载速度
        #
        # curl:
        # speed_download = bytes/sec
        #
        # 转换：
        # Mbps = bytes/s × 8 / 1,000,000
        # =================================================

        if speed_bytes <= 0:

            print(
                f"{ip} 下载速度为 0"
            )

            return 0.0

        speed_mbps = (
            speed_bytes
            * 8
            / 1_000_000
        )

        speed_mbps = round(
            speed_mbps,
            2
        )

        print(
            f"{ip} 测速成功："
            f"{speed_mbps} Mbps"
            f" | {total_time:.2f}s"
        )

        return speed_mbps

    except subprocess.TimeoutExpired:

        print(
            f"{ip} 测速超时"
        )

        return 0.0

    except Exception as e:

        print(
            f"{ip} 单次测速异常："
            f"{e}"
        )

        return 0.0


# =========================================================
# 多次测速
# =========================================================

def test_ip_speed(ip):
    """
    多次测速。

    每轮失败会重试。
    最终取成功结果的中位数。
    """

    results = []

    print(
        "\n"
        + "-" * 50
    )

    print(
        f"开始测速：{ip}"
    )

    print(
        "-" * 50
    )

    for round_no in range(
        TEST_ROUNDS
    ):

        speed = 0.0

        # =================================================
        # 重试
        # =================================================

        for retry in range(
            SPEED_RETRIES
        ):

            speed = single_speed_test(
                ip
            )

            if speed > 0:

                break

            print(
                f"{ip} 第 "
                f"{round_no + 1} 轮测速失败，"
                f"重试 "
                f"{retry + 1}/"
                f"{SPEED_RETRIES}"
            )

        # =================================================
        # 成功
        # =================================================

        if speed > 0:

            results.append(
                speed
            )

            print(
                f"{ip} 第 "
                f"{round_no + 1}/"
                f"{TEST_ROUNDS} 次："
                f"{speed} Mbps"
            )

        else:

            print(
                f"{ip} 第 "
                f"{round_no + 1}/"
                f"{TEST_ROUNDS}：失败"
            )

    # =====================================================
    # 全部失败
    # =====================================================

    if not results:

        print(
            f"{ip}：测速全部失败"
        )

        return 0.0

    # =====================================================
    # 中位数
    # =====================================================

    median_speed = statistics.median(
        results
    )

    median_speed = round(
        median_speed,
        2
    )

    print(
        f"{ip} 最终速度："
        f"{median_speed} Mbps"
    )

    return median_speed


# =========================================================
# 更新 DNS
# =========================================================

def update_dns_record(
    record,
    new_ip
):
    """
    更新 Cloudflare DNS。

    尽量保留：

    TTL
    proxied
    comment
    tags
    """

    record_id = record["id"]

    old_ip = record.get(
        "content",
        ""
    )

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

    # 保留 comment
    if "comment" in record:

        data["comment"] = record[
            "comment"
        ]

    # 保留 tags
    if "tags" in record:

        data["tags"] = record[
            "tags"
        ]

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

        print(
            response.text
        )

    except Exception as e:

        print(
            f"DNS 更新异常："
            f"{e}"
        )

        traceback.print_exc()

    return False


# =========================================================
# 主程序
# =========================================================

def main():

    print("=" * 60)

    print(
        "Cloudflare DNS 智能优选 IP"
    )

    print(
        "测速方式：curl --resolve"
    )

    print(
        f"更换阈值："
        f"{MIN_SPEED_IMPROVEMENT * 100:.0f}%"
    )

    print("=" * 60)

    # =====================================================
    # 检查环境变量
    # =====================================================

    if not CF_API_TOKEN:

        print(
            "错误：CF_API_TOKEN 未设置"
        )

        send_telegram(
            "❌ <b>Cloudflare IP 检测失败</b>\n\n"
            "CF_API_TOKEN 未设置。"
        )

        return

    if not CF_ZONE_ID:

        print(
            "错误：CF_ZONE_ID 未设置"
        )

        send_telegram(
            "❌ <b>Cloudflare IP 检测失败</b>\n\n"
            "CF_ZONE_ID 未设置。"
        )

        return

    if not CF_DNS_NAME:

        print(
            "错误：CF_DNS_NAME 未设置"
        )

        send_telegram(
            "❌ <b>Cloudflare IP 检测失败</b>\n\n"
            "CF_DNS_NAME 未设置。"
        )

        return

    # =====================================================
    # 检查 curl
    # =====================================================

    if not check_curl():

        send_telegram(
            "❌ <b>Cloudflare IP 检测失败</b>\n\n"
            "GitHub Actions 环境中没有找到 curl。"
        )

        return

    # =====================================================
    # 获取当前 DNS
    # =====================================================

    records = get_dns_records()

    if not records:

        print(
            f"没有找到 "
            f"{CF_DNS_NAME} 的 A 记录"
        )

        send_telegram(
            "❌ <b>Cloudflare IP 检测失败</b>\n\n"
            f"域名："
            f"<code>{CF_DNS_NAME}</code>\n\n"
            "无法获取 Cloudflare A 记录。"
        )

        return

    # =====================================================
    # 当前记录
    # =====================================================

    record = records[0]

    current_ip = record.get(
        "content",
        ""
    )

    if not current_ip:

        print(
            "当前 DNS IP 为空"
        )

        send_telegram(
            "❌ <b>Cloudflare IP 检测失败</b>\n\n"
            f"域名："
            f"<code>{CF_DNS_NAME}</code>\n\n"
            "当前 DNS IP 为空。"
        )

        return

    print(
        f"\n当前 DNS："
        f"{CF_DNS_NAME}"
    )

    print(
        f"当前 IP："
        f"{current_ip}"
    )

    # =====================================================
    # 测试当前 IP
    # =====================================================

    current_speed = test_ip_speed(
        current_ip
    )

    # =====================================================
    # 当前 IP 测速失败
    # =====================================================

    if current_speed <= 0:

        print(
            "\n当前 IP 测速失败。"
        )

        print(
            "为防止误换 IP，"
            "本次保持不变。"
        )

        send_telegram(
            "⚠️ <b>Cloudflare IP 检测</b>\n\n"
            f"域名："
            f"<code>{CF_DNS_NAME}</code>\n"
            f"当前 IP："
            f"<code>{current_ip}</code>\n\n"
            "❌ 当前 IP 测速失败\n"
            "⏸️ 为防止误换 IP，"
            "本次保持不变。"
        )

        return

    # =====================================================
    # 获取候选 IP
    # =====================================================

    candidates = get_candidate_ips()

    if not candidates:

        print(
            "没有获取到候选 IP。"
        )

        send_telegram(
            "⚠️ <b>Cloudflare IP 检测</b>\n\n"
            f"域名："
            f"<code>{CF_DNS_NAME}</code>\n"
            f"当前 IP："
            f"<code>{current_ip}</code>\n"
            f"当前速度："
            f"<b>{current_speed} Mbps</b>\n\n"
            "❌ 没有获取到候选优选 IP\n"
            "⏸️ 保持当前解析。"
        )

        return

    # =====================================================
    # 删除当前 IP
    # =====================================================

    candidates = [
        ip
        for ip in candidates
        if ip != current_ip
    ]

    # =====================================================
    # 没有新的候选
    # =====================================================

    if not candidates:

        print(
            "没有新的候选 IP。"
        )

        send_telegram(
            "ℹ️ <b>Cloudflare IP 检测</b>\n\n"
            f"域名："
            f"<code>{CF_DNS_NAME}</code>\n"
            f"当前 IP："
            f"<code>{current_ip}</code>\n"
            f"当前速度："
            f"<b>{current_speed} Mbps</b>\n\n"
            "没有新的候选 IP。\n"
            "⏸️ 保持当前解析。"
        )

        return

    # =====================================================
    # 测试候选
    # =====================================================

    speed_results = []

    for ip in candidates:

        speed = test_ip_speed(
            ip
        )

        if speed > 0:

            speed_results.append(
                (
                    ip,
                    speed
                )
            )

    # =====================================================
    # 所有候选失败
    # =====================================================

    if not speed_results:

        print(
            "\n所有候选 IP 测速失败。"
        )

        send_telegram(
            "⚠️ <b>Cloudflare IP 检测</b>\n\n"
            f"域名："
            f"<code>{CF_DNS_NAME}</code>\n"
            f"当前 IP："
            f"<code>{current_ip}</code>\n"
            f"当前速度："
            f"<b>{current_speed} Mbps</b>\n\n"
            "❌ 所有候选 IP 测速失败\n"
            "⏸️ 保持当前解析。"
        )

        return

    # =====================================================
    # 按速度排序
    # =====================================================

    speed_results.sort(
        key=lambda x: x[1],
        reverse=True
    )

    best_ip, best_speed = (
        speed_results[0]
    )

    # =====================================================
    # 计算速度提升
    # =====================================================

    improvement = (
        best_speed - current_speed
    ) / current_speed

    improvement_percent = round(
        improvement * 100,
        2
    )

    # =====================================================
    # 输出最终结果
    # =====================================================

    print("\n" + "=" * 60)

    print(
        f"当前 IP："
        f"{current_ip}"
    )

    print(
        f"当前速度："
        f"{current_speed} Mbps"
    )

    print(
        f"最快候选："
        f"{best_ip}"
    )

    print(
        f"候选速度："
        f"{best_speed} Mbps"
    )

    print(
        f"速度提升："
        f"{improvement_percent}%"
    )

    print(
        f"更换阈值："
        f"{MIN_SPEED_IMPROVEMENT * 100:.0f}%"
    )

    print("=" * 60)

    # =====================================================
    # 判断是否更换
    # =====================================================

    should_change = (
        best_speed > current_speed
        and improvement >= MIN_SPEED_IMPROVEMENT
    )

    # =====================================================
    # 更换 IP
    # =====================================================

    if should_change:

        print(
            "\n发现更快 IP。"
        )

        print(
            "准备更新 Cloudflare DNS。"
        )

        success = update_dns_record(
            record,
            best_ip
        )

        # =================================================
        # DNS 更新成功
        # =================================================

        if success:

            message = (
                "🚀 <b>Cloudflare IP 已优化</b>\n\n"

                f"域名："
                f"<code>{CF_DNS_NAME}</code>\n\n"

                f"旧 IP："
                f"<code>{current_ip}</code>\n"

                f"旧速度："
                f"<b>{current_speed} Mbps</b>\n\n"

                f"新 IP："
                f"<code>{best_ip}</code>\n"

                f"新速度："
                f"<b>{best_speed} Mbps</b>\n\n"

                f"📈 速度提升："
                f"<b>{improvement_percent}%</b>\n"

                f"🎯 更换阈值："
                f"<b>{MIN_SPEED_IMPROVEMENT * 100:.0f}%</b>\n\n"

                "✅ 已自动更换 IP"
            )

        # =================================================
        # DNS 更新失败
        # =================================================

        else:

            message = (
                "❌ <b>Cloudflare DNS 更新失败</b>\n\n"

                f"域名："
                f"<code>{CF_DNS_NAME}</code>\n\n"

                f"当前 IP："
                f"<code>{current_ip}</code>\n"

                f"当前速度："
                f"<b>{current_speed} Mbps</b>\n\n"

                f"最佳候选："
                f"<code>{best_ip}</code>\n"

                f"候选速度："
                f"<b>{best_speed} Mbps</b>\n\n"

                f"📈 速度提升："
                f"<b>{improvement_percent}%</b>\n\n"

                "❌ DNS 更新失败\n"
                "⏸️ 保持原 IP"
            )

    # =====================================================
    # 不更换
    # =====================================================

    else:

        if best_speed <= current_speed:

            reason = (
                "最佳候选没有当前 IP 快。"
            )

        else:

            reason = (
                f"虽然候选 IP 更快，"
                f"但只提升 "
                f"{improvement_percent}%，"
                f"不足 "
                f"{MIN_SPEED_IMPROVEMENT * 100:.0f}% "
                f"的更换阈值。"
            )

        message = (
            "⏸️ <b>Cloudflare IP 保持不变</b>\n\n"

            f"域名："
            f"<code>{CF_DNS_NAME}</code>\n\n"

            f"当前 IP："
            f"<code>{current_ip}</code>\n"

            f"当前速度："
            f"<b>{current_speed} Mbps</b>\n\n"

            f"最佳候选："
            f"<code>{best_ip}</code>\n"

            f"候选速度："
            f"<b>{best_speed} Mbps</b>\n\n"

            f"📊 速度差："
            f"<b>{improvement_percent}%</b>\n"

            f"🎯 更换阈值："
            f"<b>{MIN_SPEED_IMPROVEMENT * 100:.0f}%</b>\n\n"

            f"📌 {reason}\n\n"

            "⏸️ 本次不更换"
        )

    # =====================================================
    # TG 推送
    # =====================================================

    send_telegram(
        message
    )

    print(
        "\n任务完成。"
    )


# =========================================================
# 程序入口
# =========================================================

if __name__ == "__main__":
    main()
