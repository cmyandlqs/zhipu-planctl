# Coding Plan 自动管理工具

定时冷启动 + 额度监控 + 飞书 Bot 远程控制

## 功能

- 每天 06:00 / 11:00 / 16:00 / 21:00 自动冷启动，覆盖 06-11 / 11-16 / 16-21 / 21-02 四个 5 小时窗口（支持飞书命令动态修改时间）
- 每 5 分钟查询一次额度，记录当前 5 小时窗口的已用百分比和重置时间
- 飞书 Bot 支持: 查额度、手动冷启动、修改冷启动时间、状态通知
- SIGHUP 热重载配置（无需重启进程）
- 窗口到期前 30 分钟飞书告警
- 日志文件按天轮转（`--log-dir`），默认只保留最近 48 小时
- 多厂商适配器架构，支持:
  - **智谱 (Zhipu)** — ✅ 已实现，支持查额度 + 冷启动
  - **OpenCode Go** — ✅ 已实现，支持冷启动（用量查询需控制台）
  - **MiniMax** — ⏳ 待实现
- 仅需 Python 标准库 + pyyaml，零外部依赖

## 快速开始

```bash
pip install -e .
cp config.yaml.example config.yaml
vi config.yaml    # 填入 provider 和 api_key
python -m zhipu_planctl
```

## CLI 用法

```
python -m zhipu_planctl [选项]

选项:
  -c, --config PATH    配置文件路径 (默认: ./config.yaml)
  --query              查询一次额度并输出 JSON，然后退出
  --once               执行一次冷启动并输出 JSON，然后退出
  --version            显示版本号
  --log-dir PATH       日志目录 (默认: ./logs)
  --log-retention-hours N
                       日志保留小时数 (默认: 48)
  --watch              实时仪表盘模式（每5秒更新额度、剩余时间等）

示例:
  python -m zhipu_planctl                           # 启动守护进程
  python -m zhipu_planctl --query                   # 查询额度
  python -m zhipu_planctl --query --log-dir /tmp    # 查询并写日志
  python -m zhipu_planctl --once                    # 手动冷启动一次
  python -m zhipu_planctl --version                 # 显示版本
  kill -HUP $(cat /tmp/zhipu-plan.pid)             # 热重载配置
```

## 配置热重载

修改 `config.yaml` 后无需重启，发送 SIGHUP 信号即可实时生效：

```bash
kill -HUP $(pgrep -f zhipu_planctl)
```

支持热重载的配置项: `cold_start_times`, `quota_check_interval`, `cold_start_model`, `cold_start_prompt`, `api_key`

> 注意: 不支持热切换 `provider`（厂商），需重启进程。

## 日志

日志按天轮转到 `--log-dir` 指定的目录，当前日志文件为 `zhipu-planctl.log`，历史文件为 `zhipu-planctl.log.YYYY-MM-DD`。

默认保留最近 48 小时日志，可通过 `--log-retention-hours` 调整。启动时会清理旧日志，长期运行跨天轮转时也会继续清理过期文件。

所有操作均记录到日志: 冷启动触发/结果、额度查询、飞书消息收发、配置热重载、异常信息。

## 项目结构

```
zhipu-planctl/
├── README.md
├── CODE_REVIEW_TODO.md       # 代码审查待办清单
├── pyproject.toml            # 项目元数据与依赖
├── config.yaml.example       # 配置示例
├── setup.sh                  # 一键安装 systemd 服务
├── requirements.txt
└── src/
    └── zhipu_planctl/        # Python 包
        ├── __init__.py
        ├── __main__.py       # 支持 python -m zhipu_planctl
        ├── cli.py            # CLI 入口与主循环
        ├── client.py         # 抽象基类 + 多厂商适配器
        ├── config.py          # YAML 配置加载
        ├── scheduler.py       # 定时调度逻辑
        └── feishu_bot.py      # 飞书 Bot 封装
```

## 部署

### 手动部署

```bash
git clone <你的仓库地址> ~/zhipu-coding-plan
cd ~/zhipu-coding-plan
python3 -m venv venv
source venv/bin/activate
pip install -e .
cp config.yaml.example config.yaml
vi config.yaml
python -m zhipu_planctl
```

### systemd 服务 (推荐)

```bash
bash setup.sh
# 按提示填入 API Key 后:
systemctl --user start zhipu-plan
systemctl --user enable zhipu-plan
```

systemd 部署后可以同时查看 journal 和文件日志:

```bash
journalctl --user -u zhipu-plan -f
tail -f ~/zhipu-coding-plan/logs/zhipu-planctl.log
```

如果希望退出 SSH 后 user service 仍保持运行，需要在服务器上执行:

```bash
loginctl enable-linger "$(whoami)"
```

安装脚本生成的 systemd service 会显式设置 `TZ=Asia/Shanghai`，因此冷启动时间按北京时间解释。若手动部署，请确认服务器本地时区或 service 的 `TZ` 与你的目标时间一致。

## 配置说明

### 切换厂商

`config.yaml` 中设置 `provider` 字段即可:

```yaml
provider: zhipu
zhipu:
  api_key: "your-key"
  cold_start_model: "glm-4.7"   # 冷启动使用的模型
  cold_start_prompt: "hi"       # 冷启动发送的 prompt
```

### 飞书配置

1. 确保已安装 [lark-cli](https://github.com/anomalyco/lark-cli) 并完成登录
2. 设置飞书通知: 在飞书中找到需要接收通知的群，获取 chat_id
3. 在 config.yaml 中填入 `feishu.notify_chat_id`
4. 启动后即可在群中通过命令交互

### 飞书 Bot 命令

| 命令 | 说明 |
|------|------|
| 查额度 / status | 查看当前 5 小时窗口额度和剩余时间 |
| 冷启动 / refresh | 手动触发一次冷启动 |
| 冷启动时间 06:00 11:00 16:00 21:00 | 修改自动冷启动时间 (SIGHUP 热重载也生效) |
| 帮助 / help | 显示命令列表 |
