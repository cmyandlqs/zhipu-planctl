# AGENTS.md

This file is the project-specific guide for agents working on `zhipu-planctl`.

## Project Goal

This tool keeps Zhipu Coding Plan active during the user's intended Beijing-time work windows:

- `06:00-11:00`
- `11:00-16:00`
- `16:00-21:00`
- `21:00-02:00`

The configured cold-start times must therefore remain:

```yaml
schedule:
  cold_start_times:
    - "06:00"
    - "11:00"
    - "16:00"
    - "21:00"
```

Do not add a `02:00` cold-start slot unless the user explicitly changes the operating model. After `02:00`, Coding Plan should not be proactively reactivated until `06:00`.

The second core requirement is that the user can query quota at any time from the Feishu Bot using commands such as `查额度`, `status`, or `quota`.

## Linux Deployment

Before changing deployment behavior, systemd settings, logging, time handling, or Feishu Bot runtime assumptions, read [LINUX_DEPLOYMENT.md](./LINUX_DEPLOYMENT.md).

Linux deployment is a first-class target. The service is expected to run 24 hours a day under `systemd --user`, so changes must preserve:

- explicit config path: `~/zhipu-coding-plan/config.yaml`
- file logs under `~/zhipu-coding-plan/logs`
- daily log rotation with 48-hour retention by default
- journal output for `journalctl --user -u zhipu-plan`
- Beijing-time scheduling via `TZ=Asia/Shanghai`
- compatibility with `lark-cli` running under the same Linux user

## Current Runtime Behavior

- CLI entry point: `python -m zhipu_planctl`
- Default log file: `logs/zhipu-planctl.log`
- Rotated log files: `logs/zhipu-planctl.log.YYYY-MM-DD`
- Default log retention: `48` hours
- Default quota polling interval: `5` minutes
- Config reload: `SIGHUP`, without provider switching
- Supported provider used by this project goal: `zhipu`

## Development Notes

- Keep the `src/` layout. Tests should run with `PYTHONPATH=src`.
- Preferred verification command:

```bash
PYTHONPATH=src python -m pytest -q
```

- When adding behavior around scheduling, logging, config loading, or Feishu command parsing, add or update regression tests.
- Do not rely on server local time being correct unless deployment explicitly sets `TZ=Asia/Shanghai`.
- Do not remove file logging just because systemd journal exists; both are intentionally supported.
- Do not make environment-variable API keys replace the whole YAML config. Environment variables should override sensitive fields while preserving `schedule` and `feishu` sections.

