"""
端到端流程调试脚本：手动走一遍"查额度"命令的完整链路。

本机未装 lark-cli → 飞书侧用 feishu_bot.py 里记录的真实 schema 模拟事件 JSON。
智谱侧全部走真实 HTTP 调用，能拿到生产响应。

用法：
    set PYTHONPATH=src && python debug_flow.py

IDE 单步调试：按每个"断点 N"标注的 file:line 在源码里打断点，
然后用 IDE 的 Debug 模式运行本脚本即可逐行跟进。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from urllib import request as url_request

from zhipu_planctl.config import load_config
from zhipu_planctl.client import create_client
from zhipu_planctl.scheduler import Scheduler
from zhipu_planctl.feishu_bot import FeishuBot
from zhipu_planctl.cli import CommandRouter, _COMMAND_MAP


def banner(num, title, file_ref):
    print(f"\n{'=' * 72}\n断点 {num}: {title}\n  位置: {file_ref}\n{'=' * 72}")


def mask(key: str) -> str:
    return f"{key[:8]}...{key[-4:]}" if key else "<空>"


def main():
    # ====================================================================
    banner(1, "加载配置", "config.py:26  load_config()")
    # ====================================================================
    cfg = load_config("config.yaml")
    safe_cfg = {
        "provider": cfg.get("provider"),
        "zhipu": {
            "api_key": mask(cfg.get("zhipu", {}).get("api_key", "")),
            "base_url": cfg.get("zhipu", {}).get("base_url"),
            "cold_start_model": cfg.get("zhipu", {}).get("cold_start_model"),
            "cold_start_prompt": cfg.get("zhipu", {}).get("cold_start_prompt"),
        },
        "schedule": cfg.get("schedule", {}),
        "feishu": cfg.get("feishu", {}),
    }
    print("load_config 返回（api_key 已脱敏）:")
    print(json.dumps(safe_cfg, ensure_ascii=False, indent=2))

    # ====================================================================
    banner(2, "工厂创建客户端", "client.py:531  create_client()")
    # ====================================================================
    client = create_client(cfg)
    print(f"根据 provider={cfg['provider']!r} 选中适配器: {type(client).__name__}")
    print(f"client.api_key  = {mask(client.api_key)}")
    print(f"client._host    = {client._host}")
    print(f"client.quota_url= {client.quota_url}")

    # ====================================================================
    banner(3, "智谱 HTTP 原始通信（手动复刻 query_quota 的网络层）",
           "client.py:147-188  query_quota()")
    # ====================================================================
    print(">>> HTTP 请求:")
    print(f"    GET {client.quota_url}")
    print(f"    Authorization: {mask(client.api_key)}   (智谱直接用完整 api_key，不是 Bearer)")
    print(f"    Content-Type: application/json")
    print("\n>>> HTTP 响应（智谱真实返回，未加工）:")

    req = url_request.Request(client.quota_url, method="GET")
    req.add_header("Authorization", client.api_key)
    req.add_header("Content-Type", "application/json")
    with url_request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    print(json.dumps(raw, ensure_ascii=False, indent=2))
    print("\n字段解读：")
    print("  success=true       → 业务成功")
    print("  data.level         → 用户套餐等级（lite/plus/...）")
    print("  data.limits[].unit → 3=5小时窗口, 6=每周限额")
    print("  percentage         → 已用百分比")
    print("  nextResetTime      → 重置时间（毫秒时间戳，UTC）")

    # ====================================================================
    banner(4, "客户端解析 → QuotaResult 对象",
           "client.py:147 query_quota() + client.py:247 _parse_tiers()")
    # ====================================================================
    result = client.query_quota()
    print("client.query_quota() 返回的 QuotaResult（标准化后的对象）:")
    print(f"  ok                = {result.ok}")
    print(f"  level             = {result.level!r}")
    print(f"  credential_valid  = {result.credential_valid}")
    print(f"  error             = {result.error!r}")
    print(f"  queried_at        = {result.queried_at}  (毫秒时间戳)")
    print(f"  tiers ({len(result.tiers)} 项):")
    for t in result.tiers:
        print(f"    - name={t.name!r:<14} utilization={t.utilization:<6} resets_at={t.resets_at!r}")

    # ====================================================================
    banner(5, "Scheduler 归一化 → cli 友好的 dict",
           "scheduler.py:222  check_quota_now()")
    # ====================================================================
    scheduler = Scheduler(
        cold_start_times=cfg["schedule"]["cold_start_times"],
        quota_check_interval_minutes=cfg["schedule"]["quota_check_interval_minutes"],
    )
    quota = scheduler.check_quota_now(client)
    print("scheduler.check_quota_now(client) 返回的 dict（飞书/日志都用这个结构）:")
    print(json.dumps(quota, ensure_ascii=False, indent=2, default=str))
    print("\n派生字段说明：")
    print("  five_hour_expired       → 当前时间是否过了 resets_at（决定要不要冷启动）")
    print("  five_hour_expiring_soon → 距重置是否 ≤30 分钟（决定要不要发到期告警）")

    # ====================================================================
    banner(6, "飞书事件 JSON（手机发消息时 lark-cli 推上来的样子）",
           "feishu_bot.py:213  _handle_event_line()")
    # ====================================================================
    fake_event = {
        "type": "im.message.receive_v1",   # 关键：字段名是 type 不是 event
        "chat_id": "oc_fake_chat_123",      # 群/私聊会话 id（回复目标）
        "chat_type": "group",               # group 或 p2p
        "sender_id": "ou_user_abc",         # 发送人 open_id
        "message_id": "om_msg_xyz",         # 消息 id
        "content": "@ZhipuBot 查额度",        # 群里 @机器人 会带前缀
        "message_type": "text",             # 只处理 text 类型
    }
    print("lark-cli event consume 的 stdout 一行（NDJSON，顶层平铺无 data 包裹）:")
    print(json.dumps(fake_event, ensure_ascii=False))

    # ====================================================================
    banner(7, "事件清洗 + 命令分发表查找",
           "feishu_bot.py:265 清洗 + cli.py:44 _COMMAND_MAP")
    # ====================================================================
    content = fake_event["content"]
    # 复刻 feishu_bot._handle_event_line 的清洗逻辑
    text = re.sub(r"^@\S+\s+", "", content).strip().lower()
    print(f"原始 content        = {content!r}")
    print(f"剥 @ + lower 后 text = {text!r}")
    print(f"_COMMAND_MAP[{text!r}] = {_COMMAND_MAP.get(text)!r}")
    print("  （命中 status handler）")

    # ====================================================================
    banner(8, "CommandRouter 端到端（拦截飞书发送，看渲染结果）",
           "cli.py:126 _handle_status() + cli.py:177 dispatch()")
    # ====================================================================
    captured = []  # 拦截 send_message，看最终要发什么

    feishu = FeishuBot(notify_chat_id=fake_event["chat_id"])
    feishu.send_message = lambda text, chat_id=None: captured.append((chat_id, text))

    router = CommandRouter(
        scheduler, client, feishu,
        cold_start_model=cfg["zhipu"]["cold_start_model"],
        cold_start_prompt=cfg["zhipu"]["cold_start_prompt"],
    )

    print(f">>> router.dispatch(text={text!r}, chat_id={fake_event['chat_id']!r}, sender_id=...)")
    router.dispatch(text, fake_event["chat_id"], fake_event["sender_id"])

    print(f"\n路由后由 _handle_status 调 feishu.send_status → 最终发到飞书的消息:")
    for cid, msg in captured:
        print(f"  [chat_id={cid}]")
        print("  " + msg.replace("\n", "\n  "))

    # ====================================================================
    banner(9, "冷启动流程（force=False 安全演示，不会真触发）",
           "scheduler.py:272 cold_start_if_needed()")
    # ====================================================================
    print("cold_start_if_needed(client, model='glm-4.7', prompt='hi', force=False)")
    print("  → 先查额度 → 判断 five_hour_expired → 过期才启动")
    cs_result = scheduler.cold_start_if_needed(
        client,
        model=cfg["zhipu"]["cold_start_model"],
        prompt=cfg["zhipu"]["cold_start_prompt"],
        force=False,
    )
    print(f"\n返回（force=False，因为窗口未过期所以跳过）:")
    print(json.dumps(cs_result, ensure_ascii=False, indent=2, default=str))
    print("\n定时触发（06:00/11:00/16:00/21:00）走 force=True，最多重试 10 次，")
    print("每次成功后 sleep 2s 再查额度验证窗口确实刷新了（见 scheduler.py:308-337）。")

    # ====================================================================
    banner(10, "完整链路总结", "")
    # ====================================================================
    print("""
手机发"查额度"
      │
      ▼
[lark-cli event consume]  长连接收到飞书事件
      │  一行 JSON: {type, chat_id, content:"@Bot 查额度", ...}
      ▼
feishu_bot._listen_loop  →  _handle_event_line          feishu_bot.py:287/213
      │  剥 @ + lower → "查额度"
      ▼
_command_handler(text, chat_id, sender_id)              feishu_bot.py:271
      │
      ▼
cli.handle_command → router.dispatch                    cli.py:272/177
      │  _COMMAND_MAP["查额度"] = "status"
      ▼
CommandRouter._handle_status                            cli.py:126
      │
      ▼
scheduler.check_quota_now(client)                       scheduler.py:222
      │
      ▼
client.query_quota()  →  HTTP GET /api/monitor/usage/quota/limit   client.py:147
      │  Authorization: <api_key>
      ▼
智谱服务器返回 JSON → _parse_tiers → QuotaResult        client.py:247
      │
      ▼
scheduler 归一化为 dict（加 five_hour_expired 等派生字段） scheduler.py:222
      │
      ▼
feishu.send_status → 拼消息字符串 → send_message        feishu_bot.py:129/70
      │
      ▼
subprocess: lark-cli im +messages-send --chat-id ... --text ...   feishu_bot.py:80
      │
      ▼
飞书群里显示状态卡片
""")


if __name__ == "__main__":
    main()
