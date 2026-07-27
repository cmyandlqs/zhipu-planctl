# Linux Deployment Guide

This guide describes the current supported Linux deployment for `zhipu-planctl`.

The intended production mode is a 24-hour `systemd --user` service that keeps Zhipu Coding Plan active during Beijing-time work windows and allows Feishu Bot quota queries at any time.

## Runtime Model

The service runs continuously and performs:

- cold starts at `06:00`, `11:00`, `16:00`, and `21:00`
- no proactive cold start at `02:00`
- quota checks every 5 minutes by default
- Feishu Bot event listening through `lark-cli`
- file logging plus systemd journal logging

The generated systemd service sets `TZ=Asia/Shanghai`, so the schedule is interpreted as Beijing time even if the server itself uses UTC.

## Quick Install

From the project root on the Linux server:

```bash
bash setup.sh
vi ~/zhipu-coding-plan/config.yaml
systemctl --user start zhipu-plan
systemctl --user enable zhipu-plan
```

If the user service must keep running after SSH logout, enable lingering:

```bash
loginctl enable-linger "$(whoami)"
```

## Required Configuration

Edit:

```bash
~/zhipu-coding-plan/config.yaml
```

The schedule should be:

```yaml
schedule:
  cold_start_times:
    - "06:00"
    - "11:00"
    - "16:00"
    - "21:00"
  quota_check_interval_minutes: 5
```

For Zhipu:

```yaml
provider: zhipu

zhipu:
  api_key: "your-api-key"
  base_url: "https://open.bigmodel.cn"
  cold_start_model: "glm-4.7"
  cold_start_prompt: "hi"
```

For Feishu Bot:

```yaml
feishu:
  notify_chat_id: "your-chat-id"
  enable_bot: true
  notify_threshold: 0
```

## Feishu Bot Requirements

The Bot uses `lark-cli`, not a Python SDK. Install and authenticate `lark-cli` as the same Linux user that runs the systemd user service.

Check availability:

```bash
which lark-cli
lark-cli --version
```

The service can query quota from Feishu commands such as:

- `查额度`
- `status`
- `quota`

Manual cold start commands:

- `冷启动`
- `refresh`

Changing cold-start times from Feishu is supported:

```text
冷启动时间 06:00 11:00 16:00 21:00
```

## Logs

The systemd service writes to both journal and file logs.

Follow journal logs:

```bash
journalctl --user -u zhipu-plan -f
```

Follow file logs:

```bash
tail -f ~/zhipu-coding-plan/logs/zhipu-planctl.log
```

File log behavior:

- current file: `~/zhipu-coding-plan/logs/zhipu-planctl.log`
- rotated files: `~/zhipu-coding-plan/logs/zhipu-planctl.log.YYYY-MM-DD`
- default retention: 48 hours
- retention flag: `--log-retention-hours 48`

The generated service uses absolute paths expanded from `$HOME`. It is equivalent to:

```ini
WorkingDirectory=/home/your-user/zhipu-coding-plan
Environment=TZ=Asia/Shanghai
ExecStart=/home/your-user/zhipu-coding-plan/venv/bin/python -m zhipu_planctl --config /home/your-user/zhipu-coding-plan/config.yaml --log-dir /home/your-user/zhipu-coding-plan/logs --log-retention-hours 48
StandardOutput=journal
StandardError=journal
```

## Managing The Service

Start:

```bash
systemctl --user start zhipu-plan
```

Stop:

```bash
systemctl --user stop zhipu-plan
```

Restart:

```bash
systemctl --user restart zhipu-plan
```

Status:

```bash
systemctl --user status zhipu-plan
```

Reload config after editing `config.yaml`:

```bash
systemctl --user kill -s HUP zhipu-plan
```

Provider switching is not hot-reloadable. If `provider` changes, restart the service:

```bash
systemctl --user restart zhipu-plan
```

## Important Deployment Notes

- Confirm the generated service contains `Environment=TZ=Asia/Shanghai`.
- Confirm `lark-cli` works for the same user that owns the service.
- Confirm `~/zhipu-coding-plan/config.yaml` has the four expected cold-start slots.
- Confirm no `02:00` cold-start slot is present unless intentionally requested.
- If `ZHIPU_API_KEY` is set in the environment, it should override only the API key and preserve YAML `schedule` and `feishu` settings.
- User services may stop after logout unless linger is enabled.
- `KillMode=control-group` is intentional so child processes such as `lark-cli event consume` are cleaned up with the service.

## Verification

Manual quota query:

```bash
cd ~/zhipu-coding-plan
source venv/bin/activate
python -m zhipu_planctl --config config.yaml --query
```

One forced cold start:

```bash
cd ~/zhipu-coding-plan
source venv/bin/activate
python -m zhipu_planctl --config config.yaml --once
```

Test from the source checkout:

```bash
PYTHONPATH=src python -m pytest -q
```
