# Coding Plan 自动管理工具

定时冷启动 + 额度监控 + 飞书 Bot 远程控制

## 功能

- 每天 06:00 / 11:00 / 16:00 / 21:00 自动冷启动，重新触发 5 小时窗口
- 每 5 分钟查询一次额度，记录当前 5 小时窗口的已用百分比和重置时间
- 飞书 Bot 支持: 查额度、手动冷启动、状态通知
- 多厂商适配器架构，支持:
  - **智谱 (Zhipu)** — ✅ 已实现，支持查额度 + 冷启动
  - **OpenCode Go** — ✅ 已实现，支持冷启动（用量查询需控制台）
  - **MiniMax** — ⏳ 待实现
- 仅需 Python 标准库 + pyyaml，零外部依赖

## 项目结构

```
zhipu-planctl/
├── README.md
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
        │   ├── BasePlanClient       # 抽象接口
        │   ├── ZhipuPlanClient      # 智谱适配器
        │   ├── OpenCodeGoPlanClient # OpenCode Go 适配器
        │   ├── MiniMaxPlanClient    # MiniMax 适配器 (TODO)
        │   └── create_client()      # 工厂函数
        ├── config.py          # YAML 配置加载
        ├── scheduler.py       # 定时调度逻辑
        └── feishu_bot.py      # 飞书 Bot 封装
```

## 部署

```bash
git clone <你的仓库地址> ~/zhipu-coding-plan
cd ~/zhipu-coding-plan
python3 -m venv venv
source venv/bin/activate
pip install -e .
cp config.yaml.example config.yaml
vi config.yaml      # 填入 provider 和 api_key 及飞书配置
python -m zhipu_planctl    # 启动
```

或者直接用 `setup.sh` 一键安装并注册 systemd 服务。

### 切换厂商

`config.yaml` 中设置 `provider` 字段即可:

```yaml
provider: zhipu          # 当前使用的厂商
zhipu:
  api_key: "your-key"
opencode_go:
  api_key: "your-key"
```

## 飞书配置

1. 确保已安装 [lark-cli](https://github.com/anomalyco/lark-cli) 并完成登录
2. 设置飞书通知: 在飞书中找到需要接收通知的群，获取 chat_id
3. 在 config.yaml 中填入 `feishu.notify_chat_id`
4. 启动后即可在群中通过命令交互

### 飞书 Bot 命令

| 命令 | 说明 |
|------|------|
| 查额度 / status | 查看当前 5 小时窗口额度 |
| 冷启动 / refresh | 手动触发一次冷启动 |
| 帮助 / help | 显示命令列表 |
