# DSH 接入说明（4NEC2 MCP Server）

本文说明如何把本项目作为一个 MCP server 接进 **DeepSeek Harness（DSH）**，
使用官方插件 `@deepseek-ai/dsh-mcp-client`（随 DSH 分发，无需单独安装）。

> 目标：在 DSH 对话里直接使用 `mcp__nec2__nec_run`、`mcp__nec2__nec_sweep` 等 10 个工具（含出图）。
> 名称由配置里的 `serverName` 决定：`serverName: nec2` → 前缀 `mcp__nec2__`。

---

## 1. 前置检查

在写配置之前，先确认"单独能跑"：

```powershell
cd C:\4nec2-mcp-server

# 1) venv 与依赖就绪
.\.venv\Scripts\python.exe mcp_4nec2_server.py --version      # 期望: 2.1.0

# 2) 4NEC2 引擎可见
.\.venv\Scripts\python.exe scripts\check-tools.py              # 期望: connected: 4nec2 ... + 10 个工具

# 3) 引擎路径
.\.venv\Scripts\python.exe scripts\check-tools.py --call nec_list_engines
```

任一失败说明是服务端问题，**不是 DSH 配置问题**，先修好再继续。

## 2. 认识 DSH 的配置分层

DSH 的插件清单是「内置树 + profile patch 层」合成出来的：

- 内置/provider 层：随 DSH 分发，包含 `@deepseek-ai/dsh-mcp-client` 插件本体；
- profile patch 层：`%USERPROFILE%\.dsh\profiles\<profile>\cordis.patch.yml`（本机 profile 为 `desktop`）。

patch 文件本质是一个 YAML **数组**，元素有两种用法：

| 写法 | 语义 |
| --- | --- |
| `- id: xxx` + `name` + `config` | **覆盖**已存在的插件实例（id 必须已存在，否则不生效） |
| `- insert:` + 列表 | **新增**插件实例（首次接入 MCP server 必须用这个） |

所以"加一个新的 MCP server"要用 `insert`；已经存在的条目改名/改参数才用裸 `id`。

## 3. 接入步骤（stdio，推荐）

### 3.1 追加条目

把下面这段**顶格**追加到 `cordis.patch.yml` 末尾（与现有条目同级，注意保持 YAML 缩进）：

```yaml
# ── 4NEC2 antenna simulation over MCP ──────────────────────────────────────────
# Local stdio MCP server in C:\4nec2-mcp-server (Python venv).
# Tools reach the model as mcp__nec2__<tool>, e.g. mcp__nec2__nec_run.
- insert:
    - id: mcp-nec2
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: nec2
        transport: stdio
        command: 'C:\4nec2-mcp-server\.venv\Scripts\python.exe'
        args:
          - 'C:\4nec2-mcp-server\mcp_4nec2_server.py'
          - '--4nec2-dir'
          - 'C:\4nec2'
        cwd: 'C:\4nec2-mcp-server'
        env:
          PYTHONUTF8: '1'
          PYTHONIOENCODING: 'utf-8'
          PYTHONDONTWRITEBYTECODE: '1'
          NEC4NEC_DIR: 'C:\4nec2'
          NEC2_ENGINE: 'C:\4nec2\exe\nec2dxs11k.exe'
        # nec_optimize / nec_sweep 可能跑几分钟；默认 60000 ms 不够。
        toolCallTimeoutMs: 300000
        # 4NEC2 暂时不可用时也让 harness 正常启动，只记一条错误日志。
        failOnStartupError: false
        reconnect:
          enabled: true
          initialDelayMs: 500
          maxDelayMs: 30000
          maxAttempts: 10
```

仓库里的 [../dsh/cordis.mcp-nec2.yml](../dsh/cordis.mcp-nec2.yml) 是同一份条目的副本，可直接复制。

### 3.2 自动写入（可选）

`scripts\install.ps1 -ApplyDsh` 会自动完成上面的追加：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -ApplyDsh
```

它会：

1. 先备份为 `cordis.patch.yml.bak-yyyyMMdd-HHmmss`；
2. 幂等检查（已存在 `id: mcp-nec2` / `mcp-4nec2` 或 `serverName: nec2` 则跳过，不会重复插入）；
3. 用 **UTF-8 无 BOM**、沿用原文件换行风格写入。

不加 `-ApplyDsh` 时只打印条目文本，不碰任何 DSH 文件。

### 3.3 让 DSH 生效

- 保存 `cordis.patch.yml` 后，DSH 会**原地重载**该 MCP server 连接（不需要重启 harness）；
- 若工具没出现：重启一次 DSH，或检查日志里的 `dsh-mcp-client` 报错；
- 新对话中，工具以 `mcp__nec2__` 前缀暴露，例如：

```text
mcp__nec2__nec_list_engines   mcp__nec2__nec_list_models   mcp__nec2__nec_describe
mcp__nec2__nec_run            mcp__nec2__nec_sweep         mcp__nec2__nec_parse_out
mcp__nec2__nec_build_yagi     mcp__nec2__nec_optimize
mcp__nec2__nec_plot_pattern   mcp__nec2__nec_plot_sweep
```

想让它直接看图，就在提示词里点名出图工具（PNG 会落在 `C:\4nec2-mcp-server\plots\`，DSH 会把路径写进回答）：

```text
用 mcp__nec2__nec_plot_pattern 画 478MHz_8El_Yagi.nec 在 478.875 MHz 的方向图，带 3-D 波瓣
用 mcp__nec2__nec_plot_sweep 扫 468–492 MHz、步进 1，把 SWR、增益、阻抗和 Smith 圆图一起给我
```

> 出图工具依赖 `matplotlib`（`pip install "matplotlib>=3.8"`）。server 启动时会在主线程预加载它：
> 这是为了绕开「首次 numpy C 扩展导入发生在 MCP 工作线程里会卡在 Windows 加载器锁、工具永不返回」的坑。
> 换 Python 解释器或升级 numpy 后若出图又开始卡，先确认启动日志里有
> `[4nec2-mcp] matplotlib x.y.z ready in ...s`。

## 4. 字段说明（`@deepseek-ai/dsh-mcp-client`）

| 字段 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `serverName` | 是 | — | 工具名前缀来源；须匹配 `[A-Za-z0-9_-]{1,32}`，且**所有实例唯一** |
| `transport` | 是 | — | `stdio` 或 `streamable-http`（不支持旧版 SSE） |
| `command` | stdio | — | 可执行文件路径（这里是 venv 的 `python.exe`） |
| `args` | stdio | `[]` | 字符串数组，**不经过 shell 插值**，路径里有空格也照原样传 |
| `env` | 否 | 继承 | 追加/覆盖子进程环境变量 |
| `cwd` | 否 | — | 子进程工作目录；相对路径的 `.nec` 以 `--4nec2-dir`/`NEC4NEC_DIR` 的 `models` 为基准，与 cwd 无关 |
| `url` | http | — | `streamable-http` 端点，如 `http://127.0.0.1:8765/mcp` |
| `headers` | http | — | 额外请求头 |
| `toolCallTimeoutMs` | 否 | `60000` | 单次工具调用超时；优化/扫频建议 `300000` |
| `failOnStartupError` | 否 | `false` | `false` 时启动失败不阻塞 harness，只记日志 |
| `maxInstructionBytes` | 否 | `32768` | server `instructions` 的最大字节数 |
| `reconnect` | 否 | 见下 | 断线重连策略 |

`reconnect` 默认值：

```yaml
reconnect:
  enabled: true
  initialDelayMs: 500
  maxDelayMs: 30000
  maxAttempts: 10
```

语义：延迟从 500 ms 起翻倍，封顶 30 s，最多 10 次；**失败次数用尽后工具会被移除、停止重连**，
需要重载配置或重启 harness 才会恢复；连接稳定超过 `maxDelayMs` 后失败计数重置。

## 5. 备选：HTTP 传输

适合"server 常驻、多个客户端共用"或远程机器：

```powershell
# 终端 A：常驻 HTTP server
.\.venv\Scripts\python.exe mcp_4nec2_server.py --transport streamable-http --host 127.0.0.1 --port 8765
# 端点: http://127.0.0.1:8765/mcp
```

DSH 侧改用：

```yaml
- insert:
    - id: mcp-nec2
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: nec2
        transport: streamable-http
        url: 'http://127.0.0.1:8765/mcp'
        toolCallTimeoutMs: 300000
```

注意：`serverName` 仍要唯一；同一台机器不要同时让 stdio 版和 HTTP 版使用同一个 `serverName`。

## 6. 验证清单

| 检查项 | 命令 / 期望 |
| --- | --- |
| 服务端版本 | `.\.venv\Scripts\python.exe mcp_4nec2_server.py --version` → `2.1.0` |
| MCP 握手 + 工具数 | `.\.venv\Scripts\python.exe scripts\check-tools.py` → 10 个工具 |
| 引擎可用 | `nec_list_engines` → `preferred` 指向真实 exe |
| 能跑一次仿真 | `nec_run(path="478MHz_8El_Yagi.nec", freq_mhz=478.875)` → `status: ok` |
| 配置语法 | 用任意 YAML 解析器 parse 整个 `cordis.patch.yml`，必须是数组且条目数 = 原数 + 1 |
| DSH 内生效 | 新对话里能看到并调用 `mcp__nec2__*` |

## 7. 回滚

```powershell
# 列出备份
Get-ChildItem "$env:USERPROFILE\.dsh\profiles\desktop\cordis.patch.yml.bak-*"

# 回滚到最近一次备份
$latest = Get-ChildItem "$env:USERPROFILE\.dsh\profiles\desktop\cordis.patch.yml.bak-*" |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
Copy-Item $latest.FullName "$env:USERPROFILE\.dsh\profiles\desktop\cordis.patch.yml" -Force
```

## 8. 常见问题

**Q：条目加了但工具没出现。**
1. 是否用了 `- insert:`？裸 `- id:` 只覆盖已存在的 id，新插件不会出现。
2. `serverName` 是否与已有实例重名（重名会冲突）。
3. `command` / `args` 路径是否真实存在（注意 `args` 不做 shell 展开，不能写 `%USERPROFILE%`）。
4. 用 `scripts\check-tools.py` 在 DSH 之外手测；失败原因会写进 DSH 日志。

**Q：YAML 解析失败，报 `unknown escape sequence`。**
Windows 路径写在 **双引号** 里时 `\4` 是非法转义。用单引号（推荐）或不加引号；或者把路径写成 `C:/4nec2/...`。

**Q：调用 `nec_optimize` 超时。**
`toolCallTimeoutMs` 调到 300000 甚至更高；同时 `nec_optimize` 自己的 `timeout_s` 也要够（默认 240 s）。

**Q：用一阵子后工具消失。**
`reconnect.maxAttempts` 用尽。检查 server 是否因 4NEC2 被卸载/移动而启动失败，修复后重载配置。

**Q：出图工具报 `plotting needs matplotlib`。**
`C:\4nec2-mcp-server\.venv\Scripts\python.exe -m pip install "matplotlib>=3.8"`，然后杀掉现有的
`python.exe mcp_4nec2_server.py` 进程，DSH 会按 `reconnect` 策略重新拉起（约 4 s）。

**Q：`nec_plot_*` 一直转圈，最后超时。**
server 2.1.0 起会在启动时于主线程预加载 matplotlib；如果启动日志里没有
`[4nec2-mcp] matplotlib ... ready`，检查是否设了 `NEC_PLOTS_PRELOAD=0` / `--no-plots`。
旧版本把导入放在工具线程里，会卡在 Windows 加载器锁上，表现为「工具永不返回、客户端超时」。

**Q：多台机器共用一份配置。**
`command`/`args`/`NEC4NEC_DIR` 都是本机绝对路径，需要按机器修改；`serverName` 保持一致，工具名才不变。

## 9. 本机当前状态（2026-10-28）

- `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml` 已包含 `- insert:` 形式的 `mcp-nec2` 条目（第 85–109 行），
  YAML 解析通过，DSH 已加载工具，本会话中可见 `mcp__nec2__*` 共 10 个；
- 参考副本：`C:\4nec2-mcp-server\dsh\cordis.mcp-nec2.yml`；
- 本次接入由 Lead 手工写入，`install.ps1 -ApplyDsh` 可复现同样的条目。
