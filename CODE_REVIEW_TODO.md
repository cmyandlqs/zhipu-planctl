# Code Review TODO — zhipu-planctl

## P0 — 影响使用的 Bug ✅ 已修复

- [x] **scheduler.py:114-124 — 双重 sleep bug**：冷启动成功但验证 quota 仍过期时，sleep 两遍（行114 + 行124），重试间隔翻倍到4s
- [x] **cli.py:192-203 — main loop 无异常保护**：`tick()` 抛出异常直接崩溃，30s 后才能被 systemd 拉起来
- [x] **feishu_bot.py:253-276 — lark-cli event consume 无重连**：子进程挂了 Bot 失联，不会自动重启

## P1 — 逻辑/正确性问题 ✅ 已修复

- [x] **client.py:127-131 — 死代码**：`resp.status != 200` 永远不可达，urlopen 对非 2xx 直接抛 HTTPError
- [x] **client.py:284-319 — OpenCode Go quota 永远返回 0%**：无公开用量 API，硬编码 utilization=0.0 误导用户
- [x] **client.py:355-357 — 向后兼容覆盖 provider**：老格式 cfg 顶层的 api_key 会强制切换到 zhipu，无视显式设置的 provider
- [x] **scheduler.py:44 — _is_window_expired 不处理 Z 后缀**：API 返回 `2025-01-01T12:00:00Z` 时，Python < 3.11 的 fromisoformat 会崩
- [x] **cli.py:73 — fallback cfg.get(provider, cfg)**：如果 `zhipu` section 缺失，回退到整个顶层 dict，可能误读其他 field
- [x] **cli.py:135-145 — --query 失败 exit code 为 0**：脚本无法感知失败
- [x] **cli.py:135-145 — --query 无 five_hour tier 时无任何输出**

## P2 — 健壮性问题 ✅ 已修复

- [x] **feishu_bot.py:70 — env 只传 PATH+HOME**：已通过 `**os.environ` 继承全部环境变量，无需修改
- [x] **config.py:27 — FileNotFoundError 不告诉用户搜了哪些路径**
- [x] **scheduler.py:72 — check_quota_now 用本地时间**：API 返回 UTC 时间但 queried_at 用 naive datetime，无法直接对比
- [x] **scheduler.py:142-144 — 时间槽只检查 (hour, minute)**：如果 NTP 调钟导致跳过整分钟，冷启动直接丢失。±1min 容错
- [x] **scheduler.py:16-17 — _RETRY_MAX / _RETRY_DELAY_SEC 硬编码**：改为 Scheduler 构造函数参数

## P3 — 代码质量 ✅ 已修复

- [x] **feishu_bot.py — debug print() 残留**：全部改为 logging.debug()
- [x] **feishu_bot.py:191 — `import re` 写在函数里**：移到文件顶部
- [x] **feishu_bot.py:258 — `import sys` / `import time` 写在函数里**：移到文件顶部
- [x] **client.py — _parse_tiers 魔法数字**：`unit == 3` / `unit == 6` → `_ZHIPU_UNIT_FIVE_HOUR` / `_ZHIPU_UNIT_WEEKLY`
- [x] **client.py:17 — `from typing import Optional` 未使用**：已删除
- [x] **client.py:132-137 — cold_start 吞所有异常**：已加 log.exception()
- [x] **cli.py:33 — shutdown 最坏要等 30s**：主循环改为 1s × N 小块 sleep，SIGTERM 最坏等 1s
- [x] **cli.py — 每 tick 创建新 lambda 对象**：改为 functools.partial
- [x] **cli.py:113-117 — frozenset 做 dict key**：改为扁平 `_COMMAND_MAP: dict[str, str]`
- [x] **pyproject.toml + requirements.txt — 重复声明 pyyaml**：保留两处（requirements.txt 提供 pip install -r 兼容性）
- [x] **README.md — 冷启动时间文档还是旧值**：已更新
- [x] **setup.sh:18 — 拷贝 src 时包含 __pycache__/.pyc**：已添加清理步骤

## P4 — 推荐新功能

- [x] **日志写文件**：`--log-dir ./logs`，每天一个 `zhipu-planctl-YYYY-MM-DD.log`，启动时清理 >24h 旧日志
- [x] **`--version` 标志**：打印 `zhipu-planctl 1.0.0`
- [x] **Config 热重载 (SIGHUP)**：收到 SIGHUP 重新读 config.yaml，实时更新冷启动时间、间隔、key 等
- [x] **飞书命令改冷启动时间**：`@机器人 冷启动时间 06:00 11:00 16:00 21:00` 或 `@机器人 改时间 06:00 ...`
- [x] **窗口到期前告警**：重置前 30min 飞书推送提醒
- [x] **环境变量读 API Key**：`config.py` 支持 `ZHIPU_API_KEY` 等环境变量覆盖文件配置
- [ ] **CLI 面板模式 `--watch`**：终端实时展示额度、窗口剩余时间、下次冷启动倒计时
- [ ] **用量统计历史**：每天记录用量峰值和总用量到 CSV
- [ ] **多 provider 同时管理**：`@查额度 zhipu` / `@查额度 opencode`
- [ ] **systemd timer 代替常驻**：在 06:00/11:00/16:00/21:00 触发 service 做一次冷启动后退出
