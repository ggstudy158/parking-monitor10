#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 湘ADG3397 停车费账单监控器
# - 每 10 分钟轮询两个链接，解析服务器直接写入的隐藏字段
# - 检测「在停金额 / 订单号 / 历史欠费列表」的变化
# - 变化时写入 changes.log，并可选推送 webhook
#
# 用法:
# python3 parking_monitor.py --once            # 立即跑一次并显示解析结果(基线)
# python3 parking_monitor.py --loop            # 每 600 秒循环监控(默认)
# python3 parking_monitor.py --loop --webhook https://your-webhook  # 变化时推送
# python3 parking_monitor.py --loop --interval 600

import re
import json
import time
import sys
import os
import datetime
import urllib.request
import urllib.error
import hmac
import hashlib
import base64
import urllib.parse

# ---------------- 配置 ----------------
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0")

BASE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(BASE, "snapshot.json")
LOG_FILE = os.path.join(BASE, "changes.log")

URL_PLATEPAY = ("https://iot.cszhjt.com/wx/inpark/platePay"
                "?userId=1953&plateNo=%E6%B9%98ADG3397&plateColor=3")
URL_ARREARS = ("https://iot.cszhjt.com/wx/ttpark/order/newArrearsPayList"
               "?userId=1953&plateNo=%E6%B9%98ADG3397&plateColor=3&platePay=1")

DEFAULT_INTERVAL = 600  # 10 分钟


# ---------------- 抓取与解析 ----------------
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_platepay(html):
    # 在停支付页：当前在停金额、订单号、历史欠费笔数(首页提示)
    data = {}
    m = re.search(r'roder-info-amount-arrearsAmount"[^>]*>\s*<span>([\d.]+)</span>', html)
    if not m:
        m = re.search(r'<p class="amount">([\d.]+)</p>', html)
    data["currentAmount"] = m.group(1) if m else None

    m = re.search(r'订单编号：\s*<span>([^<]+)</span>', html)
    data["orderId"] = m.group(1).strip() if m else None

    m = re.search(r'历史未缴订单信息\s*<div class="tips">\s*<p>(\d+)</p>', html)
    data["historyCountHint"] = m.group(1) if m else None

    m = re.search(r'hidPlateNo"[^>]*value="([^"]*)"', html)
    data["plateNo"] = m.group(1) if m else None
    return data


def parse_arrears(html):
    # 欠费补缴页：完整欠费列表(含泊位号/时间/脱敏姓名) + 合计
    out = {"arrearsList": [], "historyAmount": None, "historyCount": None}
    m = re.search(r'hidArrearsList"[^>]*value="([^"]*)"', html)
    if m:
        raw = m.group(1).replace("&quot;", '"')
        try:
            out["arrearsList"] = json.loads(raw)
        except Exception:
            out["arrearsList"] = []
    m = re.search(r'hidHistoryArrearsAmount"[^>]*value="([^"]*)"', html)
    out["historyAmount"] = m.group(1) if m else None
    m = re.search(r'hidHistoryArrearsCount"[^>]*value="([^"]*)"', html)
    out["historyCount"] = m.group(1) if m else None
    return out


def collect():
    # 抓取两页并汇总成可比较的快照字典
    snap = {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        snap["platePay"] = parse_platepay(fetch(URL_PLATEPAY))
    except Exception as e:
        snap["platePay"] = {"error": str(e)}
    try:
        snap["arrears"] = parse_arrears(fetch(URL_ARREARS))
    except Exception as e:
        snap["arrears"] = {"error": str(e)}
    return snap


# ---------------- 变化检测 ----------------
def normalize_for_compare(snap):
    # 只抽取用于比较的关键字段，忽略抓取时间戳
    pp = snap.get("platePay", {})
    ar = snap.get("arrears", {})
    return {
        "currentAmount": pp.get("currentAmount"),
        # 注意: 订单号每次刷新由服务器现生成(前缀为时间戳)，不稳定，不参与变化判定
        "historyAmount": ar.get("historyAmount"),
        "historyCount": ar.get("historyCount"),
        # 欠费列表用 id+金额 组合做指纹，新增/删除/改金额都能发现
        "arrearsFingerprint": sorted([
            f"{it.get('id')}:{it.get('arrearsAmount')}:{it.get('parkingNo')}"
            for it in ar.get("arrearsList", [])
        ]),
    }


def detect_changes(old_cmp, new_cmp):
    # 返回人类可读的变化列表。无历史快照时仅静默建立基线，不推送。
    changes = []
    if old_cmp is None:
        return []  # 首次建立基线，静默处理，避免云端无持久化时反复刷屏

    if old_cmp.get("currentAmount") != new_cmp.get("currentAmount"):
        changes.append(
            f"在停金额变化: {old_cmp.get('currentAmount')} 元 → {new_cmp.get('currentAmount')} 元")
    if old_cmp.get("historyAmount") != new_cmp.get("historyAmount"):
        changes.append(
            f"历史欠费合计变化: {old_cmp.get('historyAmount')} 元 → {new_cmp.get('historyAmount')} 元")
    if old_cmp.get("historyCount") != new_cmp.get("historyCount"):
        changes.append(
            f"历史欠费笔数变化: {old_cmp.get('historyCount')} 笔 → {new_cmp.get('historyCount')} 笔")

    old_fp = set(old_cmp.get("arrearsFingerprint", []))
    new_fp = set(new_cmp.get("arrearsFingerprint", []))
    added = new_fp - old_fp
    removed = old_fp - new_fp
    if added:
        changes.append(f"新增欠费订单: {', '.join(sorted(added))}")
    if removed:
        changes.append(f"已结清/消失的欠费订单: {', '.join(sorted(removed))}")
    return changes


# ---------------- 日志 / 通知 ----------------
def log_change(snap, changes):
    line = ("=" * 60 + "\n"
            f"⏰ 检测时间: {snap['ts']}\n"
            f"🔔 变化:\n" + "\n".join(f"   - {c}" for c in changes) + "\n")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    print(line, flush=True)


def dingtalk_sign(secret):
    # 钉钉加签: timestamp + HMAC-SHA256 + base64 + urlencode
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return timestamp, sign


def push_webhook(webhook, snap, changes, sign_secret=None):
    # 推送变更到机器人 webhook。
    # 自动适配: 飞书(feishu) 用 msg_type/content.text；企业微信/钉钉 用 msgtype/text.content。
    # 钉钉若启用「加签」安全设置，传入 sign_secret 会自动追加 timestamp/sign 参数。
    text = f"【湘ADG3397 停车账单更新】\n时间: {snap['ts']}\n" + "\n".join(f"- {c}" for c in changes)
    lower = webhook.lower()
    if "feishu" in lower or "larksuite" in lower:
        payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    else:
        payload = json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
    if sign_secret:
        ts, sign = dingtalk_sign(sign_secret)
        sep = "&" if "?" in webhook else "?"
        webhook = f"{webhook}{sep}timestamp={ts}&sign={sign}"
    try:
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=10)
        print(f"[webhook 推送成功] {snap['ts']}", flush=True)
    except Exception as e:
        print(f"[webhook 推送失败] {e}", flush=True)


def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_snapshot(snap):
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def pretty(snap):
    pp = snap.get("platePay", {})
    ar = snap.get("arrears", {})
    s = [f"📅 {snap['ts']}",
         f"车牌: {pp.get('plateNo')}",
         f"当前在停金额: {pp.get('currentAmount')} 元 (订单号 {pp.get('orderId')})",
         f"历史欠费合计: {ar.get('historyAmount')} 元 / {ar.get('historyCount')} 笔"]
    for it in ar.get("arrearsList", []):
        s.append(f"  · 泊位 {it.get('parkingNo')} | 车主 {it.get('roadName')} "
                 f"| {it.get('driveInTime')}→{it.get('driveOutTime')} "
                 f"| {it.get('parkingTime')} | {it.get('arrearsAmount')}元")
    return "\n".join(s)


# ---------------- 主流程 ----------------
def main():
    args = sys.argv[1:]
    once = "--once" in args
    check = "--check" in args
    loop = "--loop" in args
    if not (once or check or loop):
        loop = True  # 默认进入循环模式
    interval = DEFAULT_INTERVAL
    if "--interval" in args:
        i = args.index("--interval")
        if i + 1 < len(args):
            interval = int(args[i + 1])
    webhook = None
    if "--webhook" in args:
        i = args.index("--webhook")
        if i + 1 < len(args):
            webhook = args[i + 1]
    sign_secret = None
    if "--sign-secret" in args:
        i = args.index("--sign-secret")
        if i + 1 < len(args):
            sign_secret = args[i + 1]
    # 从配置文件读取(本地运行用，避免密钥暴露在命令行)
    if "--config" in args:
        i = args.index("--config")
        if i + 1 < len(args):
            try:
                with open(args[i + 1], encoding="utf-8") as f:
                    cfg = json.load(f)
                webhook = cfg.get("webhook", webhook)
                sign_secret = cfg.get("sign_secret", sign_secret)
            except Exception as e:
                print(f"[配置读取失败] {e}", flush=True)
    # 从环境变量读取(GitHub Actions Secrets 通过 env 注入；优先级: 参数 > 配置文件 > 环境变量)
    if not webhook:
        webhook = os.environ.get("DINGTALK_WEBHOOK")
    if not sign_secret:
        sign_secret = os.environ.get("DINGTALK_SECRET")

    # 测试推送模式: 发一条测试消息后立即退出
    if "--test" in args:
        if not webhook:
            print("缺少 --webhook 或 --config，无法测试", flush=True)
            return
        test_snap = {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        push_webhook(webhook, test_snap,
                     ["✅ 停车监控推送测试成功，webhook 已连通（含加签）"], sign_secret)
        return

    if once:
        snap = collect()
        print(pretty(snap))
        save_snapshot(snap)
        return

    # 单次检查模式(供云端定时任务调用): 检查一次、有变化则推送、退出
    if check:
        snap = collect()
        prev = load_snapshot()
        prev_cmp = normalize_for_compare(prev) if prev else None
        new_cmp = normalize_for_compare(snap)
        changes = detect_changes(prev_cmp, new_cmp)
        if changes:
            log_change(snap, changes)
            if webhook:
                push_webhook(webhook, snap, changes, sign_secret)
        else:
            print(f"[{snap['ts']}] 无变化", flush=True)
        save_snapshot(snap)
        return

    print(f"▶ 开始监控 (间隔 {interval}s, webhook={'已配置' if webhook else '无'})", flush=True)
    prev = load_snapshot()
    prev_cmp = normalize_for_compare(prev) if prev else None
    while True:
        try:
            snap = collect()
            new_cmp = normalize_for_compare(snap)
            changes = detect_changes(prev_cmp, new_cmp)
            if changes:
                log_change(snap, changes)
                if webhook:
                    push_webhook(webhook, snap, changes, sign_secret)
            else:
                print(f"[{snap['ts']}] 无变化", flush=True)
            save_snapshot(snap)
            prev_cmp = new_cmp
        except Exception as e:
            print(f"[{datetime.datetime.now()}] 抓取异常: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
