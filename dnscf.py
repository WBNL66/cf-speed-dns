#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
精彩迪迦 Cloudflare 优选 DNS + Telegram Bot

运行模式：

GitHub Actions:
    python dnscf.py

VPS Telegram Bot:
    python dnscf.py listen

核心功能：

1. 获取当前 Cloudflare DNS A 记录
2. 获取 ipTop.html 优选 IP
3. curl --resolve 真实 HTTPS 下载测速
4. 正确使用 speed.cloudflare.com TLS SNI
5. 每个 IP 测速 2 次，取中位数
6. 候选 IP 至少比当前 IP 快 10% 才允许更换
7. 当前 IP 测速失败绝不自动更换
8. 自动任务始终发送 Telegram 结果
9. DNS 更新失败发送 Telegram
10. 保留 Cloudflare 原有 DNS 属性
11. Telegram Bot 可以手动测速
12. Telegram Bot 可以手动更换线路
13. Telegram Bot 更换前必须确认
14. Telegram Bot 支持取消更换
15. Telegram Bot 支持 DNS 回滚
16. Telegram Bot 支持历史记录
17. Telegram Bot 支持自动优化开关
18. 所有测速数据均为真实数据
"""

import os
import re
import sys
import json
import time
import signal
import socket
import shutil
import statistics
import subprocess
import threading
import traceback
from datetime import datetime, timezone, timedelta

import requests


# =========================================================
# 基础配置
# =========================================================

DEFAULT_TIMEOUT = 20

MIN_SPEED_IMPROVEMENT = 0.10

TEST_ROUNDS = 2

TEST_BYTES = 5 * 1024 * 1024

MAX_CANDIDATES = 5

SPEED_TIMEOUT = 20

SPEED_RETRIES = 2

SPEED_HOST = "speed.cloudflare.com"

CANDIDATE_URL = "https://ip.164746.xyz/ipTop.html"

DATA_FILE = os.environ.get(
    "CF_SPEED_DATA_FILE",
    "/opt/cf-speed-dns/data.json"
)

LOCK_FILE = "/tmp/cf-speed-dns.lock"


# =========================================================
# 环境变量
# =========================================================

CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "").strip()

CF_DNS_NAME = os.environ.get("CF_DNS_NAME", "").strip()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()

TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()


# =========================================================
# 北京时间
# =========================================================

BJ_TZ = timezone(
    timedelta(hours=8)
)


def now_bj():
    return datetime.now(
        BJ_TZ
    )


def now_string():
    return now_bj().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# =========================================================
# 全局状态
# =========================================================

DATA_LOCK = threading.Lock()

RUN_LOCK = threading.Lock()

STOP_EVENT = threading.Event()

PENDING_CHANGES = {}

BOT_OFFSET = 0


# =========================================================
# Cloudflare Headers
# =========================================================

CF_HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "Jincai-Dijia-CF-Optimizer/1.0"
}


# =========================================================
# HTML 转义
# =========================================================

def html_escape(value):
    value = str(value)

    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# =========================================================
# 文件数据
# =========================================================

DEFAULT_DATA = {
    "auto_enabled": True,
    "last_result": None,
    "history": []
}


def ensure_data_dir():
    directory = os.path.dirname(
        DATA_FILE
    )

    if directory:
        os.makedirs(
            directory,
            exist_ok=True
        )


def load_data():
    ensure_data_dir()

    if not os.path.exists(DATA_FILE):
        return dict(DEFAULT_DATA)

    try:
        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return dict(DEFAULT_DATA)

        result = dict(DEFAULT_DATA)

        result.update(data)

        return result

    except Exception as e:
        print(
            "[DATA] 读取失败:",
            repr(e)
        )

        return dict(DEFAULT_DATA)


DATA = load_data()


def save_data():
    ensure_data_dir()

    temp_file = DATA_FILE + ".tmp"

    with DATA_LOCK:

        try:

            with open(
                temp_file,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    DATA,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(
                temp_file,
                DATA_FILE
            )

        except Exception as e:

            print(
                "[DATA] 保存失败:",
                repr(e)
            )


# =========================================================
# 单实例锁
# =========================================================

def acquire_process_lock():

    if os.path.exists(LOCK_FILE):

        try:

            with open(
                LOCK_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                old_pid = f.read().strip()

            if old_pid:

                try:

                    os.kill(
                        int(old_pid),
                        0
                    )

                    print(
                        f"[LOCK] 已有 dnscf.py 实例运行 PID={old_pid}"
                    )

                    return False

                except ProcessLookupError:
                    pass

                except PermissionError:

                    print(
                        "[LOCK] 无法确认旧进程状态"
                    )

        except Exception:
            pass

    try:

        with open(
            LOCK_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(
                str(os.getpid())
            )

        return True

    except Exception as e:

        print(
            "[LOCK] 创建失败:",
            repr(e)
        )

        return False


def release_process_lock():

    try:

        if os.path.exists(
            LOCK_FILE
        ):

            os.remove(
                LOCK_FILE
            )

    except Exception:
        pass


# =========================================================
# Telegram API
# =========================================================

def tg_request(
    method,
    payload=None,
    timeout=DEFAULT_TIMEOUT
):

    if not TG_BOT_TOKEN:
        return {
            "ok": False,
            "description": "TG_BOT_TOKEN 未设置"
        }

    url = (
        f"https://api.telegram.org/"
        f"bot{TG_BOT_TOKEN}/"
        f"{method}"
    )

    try:

        response = requests.post(
            url,
            json=payload or {},
            timeout=timeout
        )

        try:
            return response.json()
        except Exception:
            return {
                "ok": False,
                "description": response.text
            }

    except Exception as e:

        print(
            "[TG] 请求失败:",
            repr(e)
        )

        return {
            "ok": False,
            "description": str(e)
        }


def send_telegram(
    message,
    reply_markup=None,
    chat_id=None
):

    target_chat = (
        chat_id
        if chat_id is not None
        else TG_CHAT_ID
    )

    if not TG_BOT_TOKEN or not target_chat:
        return False

    payload = {
        "chat_id": target_chat,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    result = tg_request(
        "sendMessage",
        payload,
        timeout=30
    )

    if result.get("ok"):
        return True

    print(
        "[TG] sendMessage 失败:",
        result
    )

    return False


def answer_callback(
    callback_id,
    text=None
):

    payload = {
        "callback_query_id": callback_id
    }

    if text:
        payload["text"] = text

    tg_request(
        "answerCallbackQuery",
        payload,
        timeout=15
    )


# =========================================================
# Telegram 键盘
# =========================================================

def main_keyboard():

    return {
        "inline_keyboard": [

            [
                {
                    "text": "🌐 当前状态",
                    "callback_data": "status"
                },
                {
                    "text": "🕑 延迟测速",
                    "callback_data": "test"
                }
            ],

            [
                {
                    "text": "🎯 更换线路",
                    "callback_data": "change"
                },
                {
                    "text": "🌀 重新部署",
                    "callback_data": "rollback"
                }
            ],

            [
                {
                    "text": "⚙️ 自动优化",
                    "callback_data": "settings"
                },
                {
                    "text": "🔎 历史记录",
                    "callback_data": "history"
                }
            ]
        ]
    }


def back_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "⬅️ 返回控制中心",
                    "callback_data": "menu"
                }
            ]
        ]
    }


def change_confirm_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ 确认更换",
                    "callback_data": "confirm_change"
                },
                {
                    "text": "❌ 取消更换",
                    "callback_data": "cancel_change"
                }
            ]
        ]
    }


def rollback_confirm_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ 确认恢复",
                    "callback_data": "confirm_rollback"
                },
                {
                    "text": "❌ 取消恢复",
                    "callback_data": "cancel_rollback"
                }
            ]
        ]
    }


# =========================================================
# Cloudflare DNS
# =========================================================

def get_dns_records():

    if not CF_API_TOKEN:
        return []

    if not CF_ZONE_ID:
        return []

    if not CF_DNS_NAME:
        return []

    url = (
        "https://api.cloudflare.com/client/v4/"
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
                "[CF] 获取 DNS 失败:",
                response.status_code
            )

            print(
                response.text
            )

            return []

        result = response.json()

        if not result.get(
            "success"
        ):

            print(
                "[CF] API 返回失败:",
                result
            )

            return []

        records = []

        for record in result.get(
            "result",
            []
        ):

            if (
                record.get("type") == "A"
                and record.get("name") == CF_DNS_NAME
            ):

                records.append(
                    record
                )

        return records

    except Exception as e:

        print(
            "[CF] 获取 DNS 异常:",
            repr(e)
        )

        traceback.print_exc()

        return []


def update_dns_record(
    record,
    new_ip
):

    if not record:
        return False

    record_id = record.get(
        "id"
    )

    if not record_id:
        return False

    old_ip = record.get(
        "content",
        ""
    )

    if old_ip == new_ip:
        return True

    url = (
        "https://api.cloudflare.com/client/v4/"
        f"zones/{CF_ZONE_ID}/dns_records/"
        f"{record_id}"
    )

    # =====================================================
    # 尽量保留 Cloudflare 原记录属性
    # =====================================================

    data = dict(record)

    # Cloudflare PUT 不应携带这些只读字段
    readonly_fields = [
        "id",
        "created_on",
        "modified_on",
        "meta",
        "proxiable",
        "zone_id",
        "zone_name",
        "data"
    ]

    for key in readonly_fields:
        data.pop(
            key,
            None
        )

    data["type"] = "A"

    data["name"] = record.get(
        "name",
        CF_DNS_NAME
    )

    data["content"] = new_ip

    data["ttl"] = record.get(
        "ttl",
        1
    )

    data["proxied"] = record.get(
        "proxied",
        False
    )

    try:

        response = requests.put(
            url,
            headers=CF_HEADERS,
            json=data,
            timeout=DEFAULT_TIMEOUT
        )

        try:
            result = response.json()
        except Exception:
            result = {}

        if (
            response.status_code == 200
            and result.get("success")
        ):

            print(
                f"[CF] DNS 更新成功 "
                f"{old_ip} -> {new_ip}"
            )

            return True

        print(
            "[CF] DNS 更新失败:",
            response.status_code
        )

        print(
            response.text
        )

    except Exception as e:

        print(
            "[CF] DNS 更新异常:",
            repr(e)
        )

        traceback.print_exc()

    return False


# =========================================================
# 获取优选 IP
# =========================================================

def get_candidate_ips():

    try:

        response = requests.get(
            CANDIDATE_URL,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "User-Agent":
                    "Mozilla/5.0 "
                    "Jincai-Dijia-CF-Optimizer"
            }
        )

        if response.status_code != 200:

            print(
                "[IP] 获取优选 IP 失败:",
                response.status_code
            )

            return []

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

        unique_ips = []

        for ip in ips:

            if ip not in unique_ips:

                unique_ips.append(
                    ip
                )

        print(
            "[IP] 获取候选:",
            unique_ips
        )

        return unique_ips[
            :MAX_CANDIDATES
        ]

    except Exception as e:

        print(
            "[IP] 获取候选异常:",
            repr(e)
        )

        traceback.print_exc()

        return []


# =========================================================
# Curl
# =========================================================

def check_curl():

    try:

        result = subprocess.run(
            [
                "curl",
                "--version"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10
        )

        if result.returncode == 0:

            print(
                "[CURL]",
                result.stdout.splitlines()[0]
            )

            return True

    except Exception as e:

        print(
            "[CURL] 检测失败:",
            repr(e)
        )

    return False


# =========================================================
# 单次真实测速
# =========================================================

def single_speed_test(ip):

    url = (
        f"https://{SPEED_HOST}"
        f"/__down?bytes={TEST_BYTES}"
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
            f"[SPEED] {ip} 开始测速"
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
                f"[SPEED] {ip} curl失败:",
                error
            )

            return 0.0

        parts = output.split("|")

        if len(parts) != 3:

            print(
                f"[SPEED] {ip} 结果异常:",
                output
            )

            return 0.0

        http_code = parts[0]

        speed_bytes = float(
            parts[1]
        )

        total_time = float(
            parts[2]
        )

        if http_code != "200":

            print(
                f"[SPEED] {ip} HTTP:",
                http_code
            )

            return 0.0

        if speed_bytes <= 0:

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
            f"[SPEED] {ip} "
            f"{speed_mbps:.2f} Mbps "
            f"{total_time:.2f}s"
        )

        return speed_mbps

    except subprocess.TimeoutExpired:

        print(
            f"[SPEED] {ip} 超时"
        )

        return 0.0

    except Exception as e:

        print(
            f"[SPEED] {ip} 异常:",
            repr(e)
        )

        return 0.0


# =========================================================
# 2 次测速取中位数
# =========================================================

def test_ip_speed(ip):

    results = []

    print(
        "\n"
        + "-" * 60
    )

    print(
        f"[SPEED] 测试 {ip}"
    )

    print(
        "-" * 60
    )

    for round_no in range(
        TEST_ROUNDS
    ):

        speed = 0.0

        for retry in range(
            SPEED_RETRIES
        ):

            speed = single_speed_test(
                ip
            )

            if speed > 0:
                break

            print(
                f"[SPEED] {ip} "
                f"第 {round_no + 1} 次失败 "
                f"重试 {retry + 1}/"
                f"{SPEED_RETRIES}"
            )

        if speed > 0:

            results.append(
                speed
            )

            print(
                f"[SPEED] {ip} "
                f"第 {round_no + 1}/"
                f"{TEST_ROUNDS}: "
                f"{speed:.2f} Mbps"
            )

        else:

            print(
                f"[SPEED] {ip} "
                f"第 {round_no + 1} 次失败"
            )

    if not results:

        print(
            f"[SPEED] {ip} 全部失败"
        )

        return 0.0

    median_speed = statistics.median(
        results
    )

    median_speed = round(
        median_speed,
        2
    )

    print(
        f"[SPEED] {ip} 中位数:"
        f" {median_speed:.2f} Mbps"
    )

    return median_speed


# =========================================================
# 历史
# =========================================================

def save_history(
    old_ip,
    new_ip,
    reason,
    old_speed=None,
    new_speed=None,
    improvement=None
):

    item = {
        "time": now_string(),
        "old_ip": old_ip,
        "new_ip": new_ip,
        "reason": reason
    }

    if old_speed is not None:
        item["old_speed"] = round(
            old_speed,
            2
        )

    if new_speed is not None:
        item["new_speed"] = round(
            new_speed,
            2
        )

    if improvement is not None:
        item["improvement"] = round(
            improvement,
            2
        )

    with DATA_LOCK:

        history = DATA.setdefault(
            "history",
            []
        )

        history.append(
            item
        )

        if len(history) > 100:
            del history[:-100]

    save_data()


# =========================================================
# 保存最后测速
# =========================================================

def save_last_result(
    current_ip,
    current_speed,
    best_ip=None,
    best_speed=None,
    improvement=None,
    deployed=False
):

    result = {
        "time": now_string(),
        "ip": current_ip,
        "speed": round(
            current_speed,
            2
        ),
        "best_ip": best_ip,
        "best_speed": (
            round(best_speed, 2)
            if best_speed is not None
            else None
        ),
        "improvement": (
            round(improvement, 2)
            if improvement is not None
            else None
        ),
        "deployed": deployed
    }

    with DATA_LOCK:
        DATA["last_result"] = result

    save_data()


# =========================================================
# 三种最终通知
# =========================================================

def build_not_deployed_message(
    current_ip,
    current_speed,
    best_ip,
    best_speed,
    improvement
):

    improvement = round(
        improvement,
        2
    )

    return (
        "🌐 <b>Cloudflare 本次未部署</b>\n\n"

        f"🔗域名："
        f"{html_escape(CF_DNS_NAME)}\n\n"

        f"🌈当前 IP："
        f"{html_escape(current_ip)}\n"

        f"⚡️当前速度："
        f"{current_speed:.2f} Mbps\n\n"

        f"🌍最佳候选："
        f"{html_escape(best_ip)}\n"

        f"⚡️候选速度："
        f"{best_speed:.2f} Mbps\n\n"

        f"📊 速度差："
        f"{improvement:.2f}%\n"

        f"🎯 更换阈值："
        f"{MIN_SPEED_IMPROVEMENT * 100:.0f}%\n\n"

        f"📌 虽然候选线路更快，但只提升 "
        f"{improvement:.2f}%，不足 "
        f"{MIN_SPEED_IMPROVEMENT * 100:.0f}% 的更换阈值。\n\n"

        "❌ Cloudflare DNS 没有更新"
    )


def build_deployed_message(
    current_ip,
    current_speed,
    best_ip,
    best_speed,
    improvement
):

    improvement = round(
        improvement,
        2
    )

    return (
        "🌐 <b>Cloudflare 本次已部署</b>\n\n"

        f"🔗域名："
        f"{html_escape(CF_DNS_NAME)}\n\n"

        f"🌈当前 IP："
        f"{html_escape(current_ip)}\n"

        f"⚡️当前速度："
        f"{current_speed:.2f} Mbps\n\n"

        f"🌍最佳候选："
        f"{html_escape(best_ip)}\n"

        f"⚡️候选速度："
        f"{best_speed:.2f} Mbps\n\n"

        f"📊 速度提升："
        f"{improvement:.2f}%\n"

        f"🎯 更换阈值："
        f"{MIN_SPEED_IMPROVEMENT * 100:.0f}%\n\n"

        f"📌 候选线路提升 "
        f"{improvement:.2f}%，达到更换条件。\n\n"

        "✅ Cloudflare DNS 部署成功"
    )


def build_deploy_error_message(
    current_ip,
    current_speed,
    best_ip,
    best_speed,
    improvement
):

    improvement = round(
        improvement,
        2
    )

    return (
        "⚠️ <b>Cloudflare 本次部署错误</b>\n\n"

        f"🔗域名："
        f"{html_escape(CF_DNS_NAME)}\n\n"

        f"🌈当前 IP："
        f"{html_escape(current_ip)}\n"

        f"⚡️当前速度："
        f"{current_speed:.2f} Mbps\n\n"

        f"🌍最佳候选："
        f"{html_escape(best_ip)}\n"

        f"⚡️候选速度："
        f"{best_speed:.2f} Mbps\n\n"

        f"📊 速度提升："
        f"{improvement:.2f}%\n"

        f"🎯 更换阈值："
        f"{MIN_SPEED_IMPROVEMENT * 100:.0f}%\n\n"

        f"📌 候选线路提升 "
        f"{improvement:.2f}%，达到更换条件。\n\n"

        "❌ 但Cloudflare DNS 部署失败"
    )


# =========================================================
# 测速核心
# =========================================================

def perform_scan():

    if not CF_API_TOKEN:
        raise RuntimeError(
            "CF_API_TOKEN 未设置"
        )

    if not CF_ZONE_ID:
        raise RuntimeError(
            "CF_ZONE_ID 未设置"
        )

    if not CF_DNS_NAME:
        raise RuntimeError(
            "CF_DNS_NAME 未设置"
        )

    if not check_curl():
        raise RuntimeError(
            "curl 不可用"
        )

    records = get_dns_records()

    if not records:

        raise RuntimeError(
            "无法获取 Cloudflare A 记录"
        )

    record = records[0]

    current_ip = record.get(
        "content",
        ""
    )

    if not current_ip:

        raise RuntimeError(
            "当前 DNS IP 为空"
        )

    print(
        f"[SCAN] 当前 DNS: "
        f"{CF_DNS_NAME}"
    )

    print(
        f"[SCAN] 当前 IP: "
        f"{current_ip}"
    )

    # =====================================================
    # 当前 IP 必须先成功测速
    # =====================================================

    current_speed = test_ip_speed(
        current_ip
    )

    if current_speed <= 0:

        print(
            "[SCAN] 当前 IP 测速失败"
        )

        return {
            "ok": False,
            "reason": "current_failed",
            "record": record,
            "current_ip": current_ip,
            "current_speed": 0.0
        }

    # =====================================================
    # 获取候选
    # =====================================================

    candidates = get_candidate_ips()

    candidates = [
        ip
        for ip in candidates
        if ip != current_ip
    ]

    if not candidates:

        return {
            "ok": False,
            "reason": "no_candidates",
            "record": record,
            "current_ip": current_ip,
            "current_speed": current_speed
        }

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

    if not speed_results:

        return {
            "ok": False,
            "reason": "candidate_failed",
            "record": record,
            "current_ip": current_ip,
            "current_speed": current_speed
        }

    speed_results.sort(
        key=lambda x: x[1],
        reverse=True
    )

    best_ip, best_speed = (
        speed_results[0]
    )

    # =====================================================
    # 计算真实提升
    # =====================================================

    improvement = (
        best_speed - current_speed
    ) / current_speed * 100

    improvement = round(
        improvement,
        2
    )

    should_change = (
        best_speed > current_speed
        and improvement >= (
            MIN_SPEED_IMPROVEMENT * 100
        )
    )

    return {
        "ok": True,
        "record": record,
        "current_ip": current_ip,
        "current_speed": current_speed,
        "best_ip": best_ip,
        "best_speed": best_speed,
        "improvement": improvement,
        "should_change": should_change
    }


# =========================================================
# 自动运行
# =========================================================

def run_automatic():

    if not RUN_LOCK.acquire(
        blocking=False
    ):

        print(
            "[RUN] 已有测速任务运行"
        )

        return False

    try:

        result = perform_scan()

        if not result.get("ok"):

            reason = result.get(
                "reason"
            )

            current_ip = result.get(
                "current_ip",
                "未知"
            )

            current_speed = result.get(
                "current_speed",
                0.0
            )

            if reason == "current_failed":

                send_telegram(
                    "⚠️ <b>Cloudflare 本次检测错误</b>\n\n"

                    f"🔗域名："
                    f"{html_escape(CF_DNS_NAME)}\n\n"

                    f"🌈当前 IP："
                    f"{html_escape(current_ip)}\n\n"

                    "❌ 当前 IP 真实测速失败\n"
                    "📌 为防止误换 IP，本次不修改 DNS"
                )

            elif reason == "no_candidates":

                send_telegram(
                    "⚠️ <b>Cloudflare 本次检测错误</b>\n\n"

                    f"🔗域名："
                    f"{html_escape(CF_DNS_NAME)}\n\n"

                    f"🌈当前 IP："
                    f"{html_escape(current_ip)}\n"

                    f"⚡️当前速度："
                    f"{current_speed:.2f} Mbps\n\n"

                    "❌ 没有获取到新的优选 IP\n"
                    "📌 Cloudflare DNS 没有更新"
                )

            elif reason == "candidate_failed":

                send_telegram(
                    "⚠️ <b>Cloudflare 本次检测错误</b>\n\n"

                    f"🔗域名："
                    f"{html_escape(CF_DNS_NAME)}\n\n"

                    f"🌈当前 IP："
                    f"{html_escape(current_ip)}\n"

                    f"⚡️当前速度："
                    f"{current_speed:.2f} Mbps\n\n"

                    "❌ 所有候选 IP 真实测速失败\n"
                    "📌 Cloudflare DNS 没有更新"
                )

            return False

        current_ip = result[
            "current_ip"
        ]

        current_speed = result[
            "current_speed"
        ]

        best_ip = result[
            "best_ip"
        ]

        best_speed = result[
            "best_speed"
        ]

        improvement = result[
            "improvement"
        ]

        should_change = result[
            "should_change"
        ]

        # =================================================
        # 达到更换条件
        # =================================================

        if should_change:

            success = update_dns_record(
                result["record"],
                best_ip
            )

            if success:

                save_history(
                    current_ip,
                    best_ip,
                    "auto_change",
                    current_speed,
                    best_speed,
                    improvement
                )

                save_last_result(
                    current_ip,
                    current_speed,
                    best_ip,
                    best_speed,
                    improvement,
                    True
                )

                send_telegram(
                    build_deployed_message(
                        current_ip,
                        current_speed,
                        best_ip,
                        best_speed,
                        improvement
                    )
                )

                return True

            save_last_result(
                current_ip,
                current_speed,
                best_ip,
                best_speed,
                improvement,
                False
            )

            send_telegram(
                build_deploy_error_message(
                    current_ip,
                    current_speed,
                    best_ip,
                    best_speed,
                    improvement
                )
            )

            return False

        # =================================================
        # 不达到更换条件
        # =================================================

        save_last_result(
            current_ip,
            current_speed,
            best_ip,
            best_speed,
            improvement,
            False
        )

        send_telegram(
            build_not_deployed_message(
                current_ip,
                current_speed,
                best_ip,
                best_speed,
                improvement
            )
        )

        return False

    except Exception as e:

        print(
            "[RUN] 自动任务异常:",
            repr(e)
        )

        traceback.print_exc()

        send_telegram(
            "⚠️ <b>Cloudflare 本次检测错误</b>\n\n"
            f"🔗域名："
            f"{html_escape(CF_DNS_NAME or '未配置')}\n\n"
            "❌ 程序执行异常\n"
            "📌 Cloudflare DNS 没有更新"
        )

        return False

    finally:

        RUN_LOCK.release()


# =========================================================
# Bot 手动测速
# =========================================================

def manual_test(chat_id):

    def worker():

        if not RUN_LOCK.acquire(
            blocking=False
        ):

            send_telegram(
                "⏳ 当前已经有测速任务正在运行，请稍候。",
                main_keyboard(),
                chat_id
            )

            return

        try:

            send_telegram(
                "⏳ <b>真实测速开始</b>\n\n"
                f"🔗域名："
                f"{html_escape(CF_DNS_NAME)}\n\n"
                f"🌐当前 DNS："
                f"<code>正在读取...</code>\n\n"
                f"📊 每个 IP 测速 {TEST_ROUNDS} 次\n"
                "📡 数据源：ip.164746.xyz\n"
                "🚀 测速方式：真实 HTTPS 下载",
                chat_id=chat_id
            )

            result = perform_scan()

            if not result.get("ok"):

                reason = result.get(
                    "reason"
                )

                if reason == "current_failed":

                    send_telegram(
                        "⚠️ <b>真实测速失败</b>\n\n"
                        f"当前 IP："
                        f"<code>{html_escape(result.get('current_ip'))}</code>\n\n"
                        "❌ 当前 IP 无法完成真实 HTTPS 测速。",
                        main_keyboard(),
                        chat_id
                    )

                elif reason == "no_candidates":

                    send_telegram(
                        "⚠️ <b>真实测速完成</b>\n\n"
                        f"当前 IP："
                        f"<code>{html_escape(result.get('current_ip'))}</code>\n"
                        f"当前速度："
                        f"<b>{result.get('current_speed', 0):.2f} Mbps</b>\n\n"
                        "❌ 没有新的优选 IP。",
                        main_keyboard(),
                        chat_id
                    )

                else:

                    send_telegram(
                        "⚠️ <b>真实测速失败</b>\n\n"
                        "没有获得有效候选测速结果。",
                        main_keyboard(),
                        chat_id
                    )

                return

            save_last_result(
                result["current_ip"],
                result["current_speed"],
                result["best_ip"],
                result["best_speed"],
                result["improvement"],
                False
            )

            send_telegram(
                "🔍 <b>真实测速完成</b>\n\n"

                f"🌐当前 IP："
                f"<code>{result['current_ip']}</code>\n"

                f"⚡️当前速度："
                f"<b>{result['current_speed']:.2f} Mbps</b>\n\n"

                f"🌍最佳候选："
                f"<code>{result['best_ip']}</code>\n"

                f"⚡️候选速度："
                f"<b>{result['best_speed']:.2f} Mbps</b>\n\n"

                f"📊速度提升："
                f"<b>{result['improvement']:.2f}%</b>\n"

                f"🎯更换阈值："
                f"<b>{MIN_SPEED_IMPROVEMENT * 100:.0f}%</b>\n\n"

                "📌 本次测速不会自动更换 DNS。",
                main_keyboard(),
                chat_id
            )

        except Exception as e:

            print(
                "[BOT TEST] 异常:",
                repr(e)
            )

            traceback.print_exc()

            send_telegram(
                "❌ <b>测速程序异常</b>\n\n"
                f"<code>{html_escape(str(e))}</code>",
                main_keyboard(),
                chat_id
            )

        finally:

            RUN_LOCK.release()

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


# =========================================================
# Bot 更换线路
# =========================================================

def manual_change_prepare(chat_id):

    def worker():

        if not RUN_LOCK.acquire(
            blocking=False
        ):

            send_telegram(
                "⏳ 当前已经有测速任务正在运行，请稍候。",
                main_keyboard(),
                chat_id
            )

            return

        try:

            send_telegram(
                "⏳ <b>正在寻找最佳线路</b>\n\n"
                "📡 正在获取优选 IP\n"
                "🚀 正在进行真实 HTTPS 测速\n"
                "📊 每个 IP 测速 2 次取中位数\n\n"
                "⚠️ 测速完成后不会自动更换，"
                "必须点击「✅ 确认更换」。",
                chat_id=chat_id
            )

            result = perform_scan()

            if not result.get("ok"):

                send_telegram(
                    "⚠️ <b>无法执行更换线路</b>\n\n"
                    "当前 IP 或候选 IP 真实测速失败。\n"
                    "📌 为防止误换，本次没有修改 DNS。",
                    main_keyboard(),
                    chat_id
                )

                return

            save_last_result(
                result["current_ip"],
                result["current_speed"],
                result["best_ip"],
                result["best_speed"],
                result["improvement"],
                False
            )

            if not result["should_change"]:

                send_telegram(
                    build_not_deployed_message(
                        result["current_ip"],
                        result["current_speed"],
                        result["best_ip"],
                        result["best_speed"],
                        result["improvement"]
                    ),
                    main_keyboard(),
                    chat_id
                )

                return

            # =================================================
            # 保存待确认更换
            # =================================================

            PENDING_CHANGES[
                str(chat_id)
            ] = {
                "time": time.time(),
                "record": result["record"],
                "current_ip": result["current_ip"],
                "current_speed": result["current_speed"],
                "best_ip": result["best_ip"],
                "best_speed": result["best_speed"],
                "improvement": result["improvement"]
            }

            send_telegram(
                "🎯 <b>最佳线路已找到</b>\n\n"

                f"🌈当前 IP："
                f"<code>{result['current_ip']}</code>\n"

                f"⚡️当前速度："
                f"{result['current_speed']:.2f} Mbps\n\n"

                f"🌍最佳候选："
                f"<code>{result['best_ip']}</code>\n"

                f"⚡️候选速度："
                f"{result['best_speed']:.2f} Mbps\n\n"

                f"📊速度提升："
                f"<b>{result['improvement']:.2f}%</b>\n"

                f"🎯更换阈值："
                f"<b>{MIN_SPEED_IMPROVEMENT * 100:.0f}%</b>\n\n"

                f"📌 候选线路提升 "
                f"{result['improvement']:.2f}%，达到更换条件。\n\n"

                "⚠️ 是否更换 DNS？",

                change_confirm_keyboard(),

                chat_id
            )

        except Exception as e:

            print(
                "[BOT CHANGE] 异常:",
                repr(e)
            )

            traceback.print_exc()

            send_telegram(
                "❌ <b>更换线路检测异常</b>\n\n"
                f"<code>{html_escape(str(e))}</code>",
                main_keyboard(),
                chat_id
            )

        finally:

            RUN_LOCK.release()

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


# =========================================================
# 确认更换
# =========================================================

def confirm_change(
    chat_id
):

    key = str(
        chat_id
    )

    pending = PENDING_CHANGES.get(
        key
    )

    if not pending:

        send_telegram(
            "⚠️ 当前没有待确认的更换任务。\n\n"
            "请重新点击「🎯 更换线路」。",
            main_keyboard(),
            chat_id
        )

        return

    # =====================================================
    # 待确认有效时间 10 分钟
    # =====================================================

    if (
        time.time()
        - pending.get("time", 0)
        > 600
    ):

        PENDING_CHANGES.pop(
            key,
            None
        )

        send_telegram(
            "⏱️ <b>确认已过期</b>\n\n"
            "请重新执行「🎯 更换线路」。",
            main_keyboard(),
            chat_id
        )

        return

    if not RUN_LOCK.acquire(
        blocking=False
    ):

        send_telegram(
            "⏳ 当前已有任务正在运行，请稍候。",
            main_keyboard(),
            chat_id
        )

        return

    try:

        # =================================================
        # 再次读取 Cloudflare 当前 DNS
        # 防止等待期间 IP 已发生变化
        # =================================================

        records = get_dns_records()

        if not records:

            send_telegram(
                "⚠️ <b>Cloudflare 本次部署错误</b>\n\n"
                "❌ 无法重新读取 Cloudflare DNS。\n"
                "📌 当前解析保持不变。",
                main_keyboard(),
                chat_id
            )

            return

        record = records[0]

        actual_current_ip = record.get(
            "content",
            ""
        )

        if (
            actual_current_ip
            != pending["current_ip"]
        ):

            PENDING_CHANGES.pop(
                key,
                None
            )

            send_telegram(
                "⚠️ <b>线路已发生变化</b>\n\n"
                f"当前实际 IP："
                f"<code>{html_escape(actual_current_ip)}</code>\n\n"
                "⚠️ 为防止覆盖其他变化，本次取消更换。\n"
                "请重新执行「🎯 更换线路」。",
                main_keyboard(),
                chat_id
            )

            return

        new_ip = pending[
            "best_ip"
        ]

        success = update_dns_record(
            record,
            new_ip
        )

        PENDING_CHANGES.pop(
            key,
            None
        )

        if success:

            save_history(
                pending["current_ip"],
                new_ip,
                "manual_change",
                pending["current_speed"],
                pending["best_speed"],
                pending["improvement"]
            )

            save_last_result(
                pending["current_ip"],
                pending["current_speed"],
                new_ip,
                pending["best_speed"],
                pending["improvement"],
                True
            )

            send_telegram(
                "🌐 <b>Cloudflare 本次已部署</b>\n\n"

                f"🔗域名："
                f"{html_escape(CF_DNS_NAME)}\n\n"

                f"🌈当前 IP："
                f"{html_escape(pending['current_ip'])}\n"

                f"⚡️当前速度："
                f"{pending['current_speed']:.2f} Mbps\n\n"

                f"🌍最佳候选："
                f"{html_escape(new_ip)}\n"

                f"⚡️候选速度："
                f"{pending['best_speed']:.2f} Mbps\n\n"

                f"📊 速度提升："
                f"{pending['improvement']:.2f}%\n"

                f"🎯 更换阈值："
                f"{MIN_SPEED_IMPROVEMENT * 100:.0f}%\n\n"

                f"📌 候选线路提升 "
                f"{pending['improvement']:.2f}%，达到更换条件。\n\n"

                "✅ Cloudflare DNS 部署成功",

                main_keyboard(),
                chat_id
            )

        else:

            send_telegram(
                "⚠️ <b>Cloudflare 本次部署错误</b>\n\n"

                f"🔗域名："
                f"{html_escape(CF_DNS_NAME)}\n\n"

                f"🌈当前 IP："
                f"{html_escape(pending['current_ip'])}\n"

                f"⚡️当前速度："
                f"{pending['current_speed']:.2f} Mbps\n\n"

                f"🌍最佳候选："
                f"{html_escape(new_ip)}\n"

                f"⚡️候选速度："
                f"{pending['best_speed']:.2f} Mbps\n\n"

                f"📊 速度提升："
                f"{pending['improvement']:.2f}%\n"

                f"🎯 更换阈值："
                f"{MIN_SPEED_IMPROVEMENT * 100:.0f}%\n\n"

                f"📌 候选线路提升 "
                f"{pending['improvement']:.2f}%，达到更换条件。\n\n"

                "❌ 但Cloudflare DNS 部署失败",

                main_keyboard(),
                chat_id
            )

    except Exception as e:

        print(
            "[BOT CONFIRM] 异常:",
            repr(e)
        )

        traceback.print_exc()

        send_telegram(
            "⚠️ <b>Cloudflare 本次部署错误</b>\n\n"
            "❌ 执行 DNS 更换时发生异常。\n"
            "📌 当前 IP 保持不变。",
            main_keyboard(),
            chat_id
        )

    finally:

        RUN_LOCK.release()


# =========================================================
# 取消更换
# =========================================================

def cancel_change(
    chat_id
):

    PENDING_CHANGES.pop(
        str(chat_id),
        None
    )

    send_telegram(
        "❌ <b>已取消更换</b>\n\n"
        "Cloudflare DNS 没有修改。",
        main_keyboard(),
        chat_id
    )


# =========================================================
# 当前状态
# =========================================================

def show_status(
    chat_id
):

    records = get_dns_records()

    if not records:

        send_telegram(
            "❌ <b>当前状态获取失败</b>\n\n"
            "无法读取 Cloudflare DNS。",
            back_keyboard(),
            chat_id
        )

        return

    record = records[0]

    current_ip = record.get(
        "content",
        "未知"
    )

    last = DATA.get(
        "last_result"
    )

    if last:

        speed = last.get(
            "speed",
            0
        )

        best_ip = last.get(
            "best_ip"
        )

        last_time = last.get(
            "time",
            "暂无"
        )

    else:

        speed = 0

        best_ip = None

        last_time = "暂无"

    auto_enabled = DATA.get(
        "auto_enabled",
        True
    )

    text = (
        "🎖️<b>精彩迪迦 优选控制中心</b>\n\n"

        f"🌐当前 DNS："
        f"{html_escape(current_ip)}\n\n"

        f"⬇️下载速度："
        f"{speed:.2f} Mbps\n\n"

        "⚡️延迟："
        "暂无\n\n"

        "📦丢包："
        "暂无\n\n"

        "🕒最后测速\n"
        f"{html_escape(last_time)}\n\n"

        "🤖自动优化："
        f"{'✅开启' if auto_enabled else '❌关闭'}"
    )

    send_telegram(
        text,
        main_keyboard(),
        chat_id
    )


# =========================================================
# 首页
# =========================================================

def show_menu(
    chat_id
):

    records = get_dns_records()

    current_ip = (
        records[0].get(
            "content",
            "未知"
        )
        if records
        else "未知"
    )

    last = DATA.get(
        "last_result"
    )

    if last:

        speed = last.get(
            "speed",
            0
        )

        last_time = last.get(
            "time",
            "暂无"
        )

    else:

        speed = 0

        last_time = "暂无"

    auto_enabled = DATA.get(
        "auto_enabled",
        True
    )

    text = (
        "🎖️<b>精彩迪迦 优选控制中心</b>\n\n"

        f"🌐当前 DNS ："
        f"{html_escape(current_ip)}\n\n"

        f"⬇️下载速度："
        f"{speed:.2f} Mbps\n\n"

        "⚡️ 延迟："
        "暂无\n\n"

        "📦 丢包："
        "暂无\n\n"

        "🕒 最后测速\n"
        f"{html_escape(last_time)}\n\n"

        "🤖 自动优化："
        f"{'✅开启' if auto_enabled else '❌关闭'}"
    )

    send_telegram(
        text,
        main_keyboard(),
        chat_id
    )


# =========================================================
# 历史记录
# =========================================================

def show_history(
    chat_id
):

    history = DATA.get(
        "history",
        []
    )

    if not history:

        send_telegram(
            "🔎 <b>历史记录</b>\n\n"
            "暂无 DNS 变更记录。",
            back_keyboard(),
            chat_id
        )

        return

    lines = [
        "🔎 <b>DNS 历史记录</b>",
        ""
    ]

    for index, item in enumerate(
        history[-10:][::-1],
        1
    ):

        old_ip = item.get(
            "old_ip",
            "未知"
        )

        new_ip = item.get(
            "new_ip",
            "未知"
        )

        reason = item.get(
            "reason",
            "change"
        )

        timestamp = item.get(
            "time",
            "未知"
        )

        improvement = item.get(
            "improvement"
        )

        line = (
            f"{index}️⃣ "
            f"<code>{html_escape(timestamp)}</code>\n"
            f"🌈 {html_escape(old_ip)}"
            f" → "
            f"{html_escape(new_ip)}\n"
            f"📌 {html_escape(reason)}"
        )

        if improvement is not None:

            line += (
                f"\n📊 提升："
                f"{float(improvement):.2f}%"
            )

        lines.append(
            line
        )

        lines.append("")

    send_telegram(
        "\n".join(lines),
        back_keyboard(),
        chat_id
    )


# =========================================================
# 回滚
# =========================================================

def rollback_prepare(
    chat_id
):

    history = DATA.get(
        "history",
        []
    )

    last_change = None

    for item in reversed(
        history
    ):

        old_ip = item.get(
            "old_ip"
        )

        new_ip = item.get(
            "new_ip"
        )

        if (
            old_ip
            and new_ip
            and old_ip != new_ip
        ):

            last_change = item

            break

    if not last_change:

        send_telegram(
            "🌀 <b>重新部署</b>\n\n"
            "没有可回滚的 DNS 记录。",
            back_keyboard(),
            chat_id
        )

        return

    PENDING_CHANGES[
        f"rollback:{chat_id}"
    ] = {
        "time": time.time(),
        "old_ip": last_change["old_ip"],
        "new_ip": last_change["new_ip"]
    }

    send_telegram(
        "🌀 <b>重新部署</b>\n\n"

        f"当前记录："
        f"<code>{html_escape(last_change['new_ip'])}</code>\n\n"

        f"恢复为："
        f"<code>{html_escape(last_change['old_ip'])}</code>\n\n"

        f"时间："
        f"<code>{html_escape(last_change.get('time', '未知'))}</code>\n\n"

        "⚠️ 是否恢复上一条 DNS 配置？",

        rollback_confirm_keyboard(),

        chat_id
    )


def confirm_rollback(
    chat_id
):

    key = f"rollback:{chat_id}"

    pending = PENDING_CHANGES.get(
        key
    )

    if not pending:

        send_telegram(
            "⚠️ 没有待确认的恢复操作。",
            main_keyboard(),
            chat_id
        )

        return

    if (
        time.time()
        - pending.get("time", 0)
        > 600
    ):

        PENDING_CHANGES.pop(
            key,
            None
        )

        send_telegram(
            "⏱️ 恢复操作已过期。",
            main_keyboard(),
            chat_id
        )

        return

    if not RUN_LOCK.acquire(
        blocking=False
    ):

        send_telegram(
            "⏳ 当前已有任务正在运行，请稍候。",
            main_keyboard(),
            chat_id
        )

        return

    try:

        records = get_dns_records()

        if not records:

            send_telegram(
                "❌ <b>重新部署失败</b>\n\n"
                "无法读取 Cloudflare DNS。",
                main_keyboard(),
                chat_id
            )

            return

        record = records[0]

        current_ip = record.get(
            "content",
            ""
        )

        target_ip = pending[
            "old_ip"
        ]

        success = update_dns_record(
            record,
            target_ip
        )

        PENDING_CHANGES.pop(
            key,
            None
        )

        if success:

            save_history(
                current_ip,
                target_ip,
                "manual_rollback"
            )

            send_telegram(
                "✅ <b>DNS 重新部署成功</b>\n\n"

                f"🔗域名："
                f"{html_escape(CF_DNS_NAME)}\n\n"

                f"🌈当前 IP："
                f"{html_escape(current_ip)}\n"

                f"🔄恢复 IP："
                f"{html_escape(target_ip)}\n\n"

                "✅ Cloudflare DNS 已恢复。",
                main_keyboard(),
                chat_id
            )

        else:

            send_telegram(
                "⚠️ <b>Cloudflare 本次部署错误</b>\n\n"

                f"🔗域名："
                f"{html_escape(CF_DNS_NAME)}\n\n"

                f"🌈当前 IP："
                f"{html_escape(current_ip)}\n"

                f"🔄目标 IP："
                f"{html_escape(target_ip)}\n\n"

                "❌ 但Cloudflare DNS 部署失败",
                main_keyboard(),
                chat_id
            )

    except Exception as e:

        print(
            "[ROLLBACK] 异常:",
            repr(e)
        )

        traceback.print_exc()

        send_telegram(
            "❌ <b>重新部署失败</b>\n\n"
            f"<code>{html_escape(str(e))}</code>",
            main_keyboard(),
            chat_id
        )

    finally:

        RUN_LOCK.release()


def cancel_rollback(
    chat_id
):

    PENDING_CHANGES.pop(
        f"rollback:{chat_id}",
        None
    )

    send_telegram(
        "❌ <b>已取消恢复</b>\n\n"
        "Cloudflare DNS 没有修改。",
        main_keyboard(),
        chat_id
    )


# =========================================================
# 设置
# =========================================================

def show_settings(
    chat_id
):

    enabled = DATA.get(
        "auto_enabled",
        True
    )

    status = (
        "✅开启"
        if enabled
        else
        "❌关闭"
    )

    button_text = (
        "🔴 关闭自动优化"
        if enabled
        else
        "🟢 开启自动优化"
    )

    keyboard = {
        "inline_keyboard": [

            [
                {
                    "text": button_text,
                    "callback_data": "toggle_auto"
                }
            ],

            [
                {
                    "text": "⬅️ 返回控制中心",
                    "callback_data": "menu"
                }
            ]
        ]
    }

    send_telegram(
        "⚙️ <b>自动优化</b>\n\n"

        f"当前状态：{status}\n\n"

        "GitHub Actions 优选时间：\n"
        "北京时间 00:00\n"
        "北京时间 09:00\n"
        "北京时间 12:00\n"
        "北京时间 17:00\n"
        "北京时间 21:00\n"
        "北京时间 22:00\n"
        "北京时间 23:00\n\n"

        "⚠️ 自动任务实际运行由 GitHub Actions 控制。",

        keyboard,
        chat_id
    )


def toggle_auto(
    chat_id
):

    DATA["auto_enabled"] = not DATA.get(
        "auto_enabled",
        True
    )

    save_data()

    show_settings(
        chat_id
    )


# =========================================================
# Callback
# =========================================================

def handle_callback(
    callback
):

    callback_id = callback.get(
        "id"
    )

    data = callback.get(
        "data",
        ""
    )

    message = callback.get(
        "message",
        {}
    )

    chat = message.get(
        "chat",
        {}
    )

    chat_id = str(
        chat.get(
            "id",
            ""
        )
    )

    if not chat_id:
        return

    if TG_CHAT_ID and chat_id != str(
        TG_CHAT_ID
    ):

        answer_callback(
            callback_id,
            "无权限"
        )

        return

    answer_callback(
        callback_id
    )

    if data == "menu":

        show_menu(
            chat_id
        )

    elif data == "status":

        show_status(
            chat_id
        )

    elif data == "test":

        manual_test(
            chat_id
        )

    elif data == "change":

        manual_change_prepare(
            chat_id
        )

    elif data == "confirm_change":

        confirm_change(
            chat_id
        )

    elif data == "cancel_change":

        cancel_change(
            chat_id
        )

    elif data == "rollback":

        rollback_prepare(
            chat_id
        )

    elif data == "confirm_rollback":

        confirm_rollback(
            chat_id
        )

    elif data == "cancel_rollback":

        cancel_rollback(
            chat_id
        )

    elif data == "history":

        show_history(
            chat_id
        )

    elif data == "settings":

        show_settings(
            chat_id
        )

    elif data == "toggle_auto":

        toggle_auto(
            chat_id
        )


# =========================================================
# Telegram 命令
# =========================================================

def handle_command(
    text,
    chat_id
):

    if not text:
        return

    command = text.split()[0].lower()

    command = command.split("@")[0]

    print(
        "[TG COMMAND]",
        command
    )

    if command in (
        "/start",
        "/menu"
    ):

        show_menu(
            chat_id
        )

    elif command in (
        "/test",
        "/speed",
        "/check"
    ):

        manual_test(
            chat_id
        )

    elif command in (
        "/change",
        "/switch"
    ):

        manual_change_prepare(
            chat_id
        )

    elif command in (
        "/status",
    ):

        show_status(
            chat_id
        )

    elif command in (
        "/history",
    ):

        show_history(
            chat_id
        )

    elif command in (
        "/rollback",
        "/deploy"
    ):

        rollback_prepare(
            chat_id
        )

    elif command == "/settings":

        show_settings(
            chat_id
        )

    elif command == "/auto":

        DATA["auto_enabled"] = True

        save_data()

        send_telegram(
            "🤖 <b>自动优化：✅开启</b>",
            main_keyboard(),
            chat_id
        )

    elif command == "/stopauto":

        DATA["auto_enabled"] = False

        save_data()

        send_telegram(
            "🤖 <b>自动优化：❌关闭</b>",
            main_keyboard(),
            chat_id
        )

    elif command == "/help":

        send_telegram(
            "📖 <b>Bot 指令</b>\n\n"

            "/start - 控制中心\n"
            "/menu - 控制中心\n"
            "/test - 真实测速\n"
            "/change - 更换线路\n"
            "/status - 当前状态\n"
            "/history - DNS 历史记录\n"
            "/rollback - 重新部署/回滚\n"
            "/settings - 自动优化\n"
            "/auto - 开启自动优化\n"
            "/stopauto - 关闭自动优化\n"
            "/help - 帮助",

            main_keyboard(),
            chat_id
        )

    else:

        send_telegram(
            "❓ 未知指令\n\n"
            "发送 /help 查看可用指令。",
            main_keyboard(),
            chat_id
        )


# =========================================================
# Telegram Listener
# =========================================================

def telegram_listener():

    global BOT_OFFSET

    print(
        "[BOT] Telegram Listener 启动"
    )

    # =====================================================
    # 删除旧 webhook
    # =====================================================

    tg_request(
        "deleteWebhook",
        {
            "drop_pending_updates": False
        },
        timeout=20
    )

    # =====================================================
    # 读取 offset
    # =====================================================

    BOT_OFFSET = 0

    if TG_CHAT_ID:

        send_telegram(
            "🟢 <b>精彩迪迦 Bot 已启动</b>\n\n"
            "发送 /start 打开控制中心。",
            main_keyboard()
        )

    while not STOP_EVENT.is_set():

        try:

            result = tg_request(
                "getUpdates",
                {
                    "offset": BOT_OFFSET,
                    "timeout": 30,
                    "allowed_updates": [
                        "message",
                        "callback_query"
                    ]
                },
                timeout=40
            )

            if not result.get("ok"):

                print(
                    "[BOT] getUpdates 失败:",
                    result
                )

                time.sleep(5)

                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                BOT_OFFSET = (
                    update.get(
                        "update_id",
                        BOT_OFFSET
                    )
                    + 1
                )

                # =================================================
                # 普通消息
                # =================================================

                if "message" in update:

                    message = update[
                        "message"
                    ]

                    chat_id = str(
                        message.get(
                            "chat",
                            {}
                        ).get(
                            "id",
                            ""
                        )
                    )

                    if (
                        TG_CHAT_ID
                        and chat_id != str(
                            TG_CHAT_ID
                        )
                    ):

                        continue

                    text = message.get(
                        "text",
                        ""
                    ).strip()

                    if text.startswith(
                        "/"
                    ):

                        handle_command(
                            text,
                            chat_id
                        )

                # =================================================
                # Callback
                # =================================================

                elif "callback_query" in update:

                    handle_callback(
                        update[
                            "callback_query"
                        ]
                    )

        except KeyboardInterrupt:

            break

        except Exception as e:

            print(
                "[BOT] Listener异常:",
                repr(e)
            )

            traceback.print_exc()

            time.sleep(5)


# =========================================================
# 信号
# =========================================================

def signal_handler(
    signum,
    frame
):

    print(
        "[SYSTEM] 收到停止信号"
    )

    STOP_EVENT.set()

    release_process_lock()


signal.signal(
    signal.SIGINT,
    signal_handler
)

signal.signal(
    signal.SIGTERM,
    signal_handler
)


# =========================================================
# GitHub / 自动任务
# =========================================================

def main():

    mode = (
        sys.argv[1].lower()
        if len(sys.argv) > 1
        else "auto"
    )

    print(
        "=" * 60
    )

    print(
        "🎖️ 精彩迪迦 Cloudflare 优选"
    )

    print(
        f"域名：{CF_DNS_NAME or '未配置'}"
    )

    print(
        "测速：curl --resolve"
    )

    print(
        "TLS SNI：speed.cloudflare.com"
    )

    print(
        f"测速次数：{TEST_ROUNDS}"
    )

    print(
        f"更换阈值："
        f"{MIN_SPEED_IMPROVEMENT * 100:.0f}%"
    )

    print(
        "=" * 60
    )

    # =====================================================
    # Bot
    # =====================================================

    if mode in (
        "listen",
        "--listen",
        "bot"
    ):

        if not TG_BOT_TOKEN:

            print(
                "[BOT] TG_BOT_TOKEN 未设置"
            )

            return 1

        if not TG_CHAT_ID:

            print(
                "[BOT] TG_CHAT_ID 未设置"
            )

            return 1

        if not acquire_process_lock():

            return 1

        try:

            telegram_listener()

        finally:

            release_process_lock()

        return 0

    # =====================================================
    # 自动执行
    # =====================================================

    if mode in (
        "auto",
        "--auto",
        "run",
        "--run"
    ):

        return 0 if run_automatic() else 0

    print(
        "用法："
    )

    print(
        "  python dnscf.py"
    )

    print(
        "  python dnscf.py auto"
    )

    print(
        "  python dnscf.py listen"
    )

    return 1


# =========================================================
# Entry
# =========================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )
