# 安装与运行

本指南先在一台 Linux 电脑上运行接收端和采集端，再说明 HTTPS、第二台设备和后台运行。命令使用 Bash 语法，从普通用户终端执行。基本流程不需要 sudo，不安装或重启其他服务。

## 1. 检查环境、取得源码

需要 Linux、Git、Python 3.11+ 和现代浏览器。Python 必须带有 `sqlite3`、`ssl` 和 Linux 的 `fcntl` 模块；项目没有需要 pip 安装的第三方包，前端也无需 Node.js 构建。当前不支持 Windows 原生采集，未验证 macOS。

```bash
python3 -c 'import sys, sqlite3, ssl, fcntl; assert sys.version_info >= (3, 11), "需要 Python 3.11+"; print(sys.version.split()[0])'
git --version
```

缺少环境时请先按发行版文档准备；不需要为了运行 SHIKI 安装 Docker、Tailscale 或 Codex。可选探测在工具缺失或权限不足时显示未知，CPU、内存、磁盘等基础采集仍可运行。

下列默认路径由你自己的用户目录决定。源码与含有密钥的运行数据分开保存；如果路径已经存在，不要覆盖，用已有安装的升级流程或选择新目录。

```bash
export SHIKI_SOURCE_DIR="$HOME/.local/share/shiki-console"
export SHIKI_STATE_DIR="$HOME/.local/state/shiki-console"
git clone https://github.com/shiki-sl/shiki-console.git "$SHIKI_SOURCE_DIR"
cd "$SHIKI_SOURCE_DIR"
python3 -m shiki --help
```

后续所有 `python3 -m shiki` 命令都在源码根目录运行。环境变量只对当前终端有效，打开新终端需重新设置它们。无需激活虚拟环境；如使用虚拟环境，后文 systemd 模板的 Python 路径也需相应修改。

## 2. 初始化一个通用接收端

下面是**首次安装**命令。`init` 拒绝覆盖已有的配置、数据库或查看密钥，只有初始化成功才复制通用配置。不要对已有接收端重新执行复制命令。

```bash
python3 -m shiki init --data-dir "$SHIKI_STATE_DIR/receiver" &&
  cp examples/receiver.local.json "$SHIKI_STATE_DIR/receiver/receiver.json"
```

`init` 本身生成的是维护者的示例设备清单；这里用仓库内的通用配置替换它，保留刚刚生成的独立密钥与数据库。配置只包含设备 ID `local` 和接收端 ID `main`，不需要维护者的硬件或账号。

运行数据包括：

| 文件 | 用途 |
| --- | --- |
| `receiver/receiver.json` | 接收端地址、允许的 Host/Origin、设备清单 |
| `receiver/viewer.key` | 浏览器只读查看密钥；不是配对码 |
| `receiver/receiver.db` | 设备注册、令牌哈希与最新快照 |
| `agent.json` | 后续生成的采集配置，含设备上报令牌 |
| `agent.state.json` | 后续生成的上报序号和投递状态 |

这些文件不应上传仓库或作为问题附件。初始化会将私有目录设为 `0700`、私有文件设为 `0600`。

默认端口为 `8765`。如被占用，先按文末“端口冲突”修改配置，再启动；不要停止不属于本次安装的服务。

在**终端 A** 启动接收端，并保持该终端打开：

```bash
python3 -m shiki serve --data-dir "$SHIKI_STATE_DIR/receiver"
```

看到 `Shiki receiver listening on configured interface` 后，本机浏览器打开 `http://127.0.0.1:8765/`。第一次页面尚未获得查看密钥，提示认证失败是预期行为。

## 3. 配对本机并发送一次真实采样

在**终端 B** 重新设置路径并进入源码目录。`SHIKI_URL` 必须与实际配置的端口相符：

```bash
export SHIKI_SOURCE_DIR="$HOME/.local/share/shiki-console"
export SHIKI_STATE_DIR="$HOME/.local/state/shiki-console"
export SHIKI_URL="http://127.0.0.1:8765"
cd "$SHIKI_SOURCE_DIR"
```

先检查接收端是否响应；这个接口不验证查看密钥，也不证明设备已上报：

```bash
python3 - <<'PY'
import json, os
from urllib.request import ProxyHandler, build_opener
with build_opener(ProxyHandler({})).open(os.environ['SHIKI_URL'] + '/api/v1/health', timeout=3) as response:
    result = json.load(response)
assert result['ok'] and result['receiver_id'] == 'main'
print('接收端响应正常：main')
PY
```

首次创建采集配置，随后生成配对文件、配对并上报。配对码默认五分钟有效，绑定 `local`；不要把它当作查看密钥。四条命令用 `&&` 连接，遇到错误即停止该组后续操作：

```bash
python3 -m shiki agent-init --config "$SHIKI_STATE_DIR/agent.json" --device local &&
  python3 -m shiki pair --data-dir "$SHIKI_STATE_DIR/receiver" --device local --out "$SHIKI_STATE_DIR/pair-code-local.json" &&
  python3 -m shiki agent-pair --config "$SHIKI_STATE_DIR/agent.json" --url "$SHIKI_URL" --receiver main --code-file "$SHIKI_STATE_DIR/pair-code-local.json" &&
  python3 -m shiki agent --config "$SHIKI_STATE_DIR/agent.json" --once
```

最后一条命令的 `delivery` 列表中，`receiver_id` 应为 `main`、`status` 应为 `ok`，退出码应为 `0`。这表示接收端确认收到采样；某个传感器仍为 N/A 并不表示投递失败。采集会读取本机进程、硬件和可用服务状态；默认不读取 Codex 会话元数据、不扫描网络、不启动被监控服务，也不执行更新。

继续运行时只执行下面这一条，不要重复初始化或配对。默认每 15 秒采样一次；按 `Ctrl+C` 停止：

```bash
python3 -m shiki agent --config "$SHIKI_STATE_DIR/agent.json"
```

## 4. 登录页面并确认实际数据

1. 用本机文本编辑器打开 `$SHIKI_STATE_DIR/receiver/viewer.key`，复制其中一行密钥。不要把密钥放在 URL、Issue、截图或公共终端输出中。
2. 浏览器打开接收端根地址，进入“设置”，填写“主接收端”和“主接收端查看密钥”，点击“保存并刷新”。
3. 打开“设备”，应看到唯一的“我的 Linux 电脑”，ID 为 `local`。检查它已注册、连接在线、采样时间接近当前时间，CPU/内存/磁盘展示真实值或具体的不可用原因。
4. 采集持续运行时，刷新页面可看到采样时间推进。只发送一次采样后，默认超过 90 秒显示过期，超过 300 秒显示离线，属于正常时效判断。

查看密钥保存在当前浏览器或 PWA 的本机存储中，关闭后仍保留。“清除本机配对”删除浏览器保存的密钥；“清除历史快照”单独清除离线快照。不要在不信任的共享浏览器上保存密钥。

如需在终端核对，可以在采集运行时执行以下只读检查。它从私有文件读取密钥，不把密钥放入命令参数或输出，只打印设备 ID、连接状态和采样时间：

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
key = (Path(os.environ['SHIKI_STATE_DIR']) / 'receiver/viewer.key').read_text().strip()
request = Request(os.environ['SHIKI_URL'] + '/api/v1/dashboard', headers={'Authorization': 'Bearer ' + key})
with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
    result = json.load(response)
device = next(item for item in result['devices'] if item['id'] == 'local')
assert device['registered'] and device['snapshot'] is not None
print({name: device[name] for name in ('id', 'connection', 'sampled_at')})
PY
```

## 5. 可选：通过 HTTPS 从手机访问

手机上的 `127.0.0.1` 是手机自身，不能用来访问电脑。先让本机流程跑通，再使用你自己的、手机可达且证书可信的 HTTPS 根地址，例如 `https://console.example.com`。该域名仅为占位符，必须替换；可以使用已有私网反向代理或 Tailnet HTTPS，不要求公网暴露。

SHIKI 不负责安装代理、修改防火墙、DNS 或路由。配置已有 HTTPS 反向代理时，上游指向 `http://127.0.0.1:8765`，保留请求的原始 Host 和 Authorization 头，避免缓存 `/api/*`，不要把 SHIKI 放到 URL 子路径下。

停止你自己启动的接收端后，编辑 `$SHIKI_STATE_DIR/receiver/receiver.json`，保留其他字段：

| 字段 | 以 `https://console.example.com` 为例 |
| --- | --- |
| `listen.host` / `listen.port` | 保持 `127.0.0.1` / `8765`，仅供本机代理连接 |
| `listen.allowed_hosts` | 在已有列表中增加 `console.example.com`；非默认 HTTPS 端口需包含 `:端口` |
| `listen.allowed_origins` | 在已有列表中增加 `https://console.example.com`，不带路径和末尾斜杠 |
| `receivers` 中 ID 为 `main` 的 `url` | 改为 `https://console.example.com` |

重新启动接收端。手机连接相应网络，打开此 HTTPS 地址并输入查看密钥。按浏览器提供的“添加到主屏幕 / 安装应用”入口安装 PWA；界面和可用性由浏览器决定。静态离线页面不代表仍能取得实时数据，后台刷新不作保证。

本机 agent 可以继续向回环地址发送。其他电脑必须使用 HTTPS。不要关闭证书验证、放宽 Host/Origin 为通配符或把查看密钥写入 URL。若选择直接 TLS 而非反向代理，须提供 `listen.tls_cert`、`listen.tls_key` 和确切监听地址；非回环 HTTP 会被配置检查拒绝。

## 6. 可选：加入第二台 Linux 设备

1. 在接收端 `receiver.json` 的 `inventory` 中复制 `local` 条目，改成独立 ID（例如 `laptop`）、名称和位置；ID 只能用字母、数字、`_`、`-`，长度 1–64，首位是字母或数字。按实际情况设置 `expected_online` 和指标。重启接收端加载新配置。
2. 在第二台电脑完成第 1 节的环境准备与源码克隆。它只需 agent，不执行接收端 `init`，也不复制任何已有设备的 `agent.json`、数据库或查看密钥。
3. 在接收端生成绑定 `laptop` 的配对文件：

```bash
python3 -m shiki pair --data-dir "$SHIKI_STATE_DIR/receiver" --device laptop --out "$SHIKI_STATE_DIR/pair-code-laptop.json"
```

4. 用你已有的受信任通道（例如 SSH/SCP）把此文件送到第二台电脑的私有目录；仅传这一份配对文件。五分钟内在第二台电脑运行，URL 换成自己的 HTTPS 接收端：

```bash
python3 -m shiki agent-init --config "$SHIKI_STATE_DIR/agent.json" --device laptop &&
  python3 -m shiki agent-pair --config "$SHIKI_STATE_DIR/agent.json" --url https://console.example.com --receiver main --code-file "$SHIKI_STATE_DIR/pair-code-laptop.json" &&
  python3 -m shiki agent --config "$SHIKI_STATE_DIR/agent.json" --once
```

5. 在接收端页面核对 `laptop` 的注册、采样时间和设备身份，然后在第二台电脑持续运行 agent。确认配对完成后可删除你刚生成和传送的配对文件，保留 `agent.json` 及其状态文件。

多个接收端需要分别初始化、分别配对，每端有独立查看密钥。仅填备用 URL 不会自动建成备用服务；跨站点故障切换需要实际部署和验证，不属于本机快速开始的完成条件。

## 7. 可选：用 systemd 用户服务持续运行

仅适用于已有 systemd 用户会话的 Linux。先完成前台配对与上报验证，再停止前台接收端和 agent，避免端口冲突或同一配置启动两次。

仓库提供两个用户服务模板：`deploy/systemd/shiki-receiver.user.service` 和 `deploy/systemd/shiki-agent.user.service`。默认源码、数据目录与上文一致，Python 路径为 `/usr/bin/python3`；自定义目录、含空格路径或虚拟环境需先按 systemd 的路径语法调整模板。只有采集功能的电脑只安装 agent 服务。

以下命令只用于**没有同名服务的新安装**。先检查 `systemctl --user cat shiki-receiver.service shiki-agent.service`，若能看到已有服务，停止本节并保留它们；也检查 `~/.config/systemd/user/` 是否已有同名文件或符号链接，不覆盖现有配置。

```bash
mkdir -p "$HOME/.config/systemd/user"
cp -i deploy/systemd/shiki-receiver.user.service "$HOME/.config/systemd/user/shiki-receiver.service"
cp -i deploy/systemd/shiki-agent.user.service "$HOME/.config/systemd/user/shiki-agent.service"
systemctl --user daemon-reload
systemctl --user enable --now shiki-receiver.service shiki-agent.service
systemctl --user status shiki-receiver.service shiki-agent.service
```

`active` 只表示进程运行，仍应按第 4 节确认新采样。排查日志使用 `journalctl --user -u shiki-receiver.service -u shiki-agent.service -n 50 --no-pager`，分享前检查并移除私密信息。

用户服务通常随用户会话运行；未登录开机即运行、退出登录后持续运行，需要管理员按本机策略启用该用户的 lingering，这不是上述 `enable` 自动保证的。不要改用 root 运行整套接收端/agent。

停止并取消本项目服务自动启动（保留数据）：

```bash
systemctl --user disable --now shiki-agent.service shiki-receiver.service
```

## 常见问题

| 现象 | 检查方式 |
| --- | --- |
| `No module named shiki` | 确认当前目录是克隆的源码根目录，或 systemd 的 `WorkingDirectory` 正确 |
| `ModuleNotFoundError: fcntl` | 使用 Linux；不能直接在 Windows 原生 Python 中运行 |
| 页面 401 | 使用对应接收端的 `viewer.key`，不是配对码或 agent 令牌 |
| 页面或配对 403 | 检查实际 Host、Origin、端口和代理传递的 Host 是否与允许列表精确匹配 |
| 配对失败 | 检查设备 ID 已在 inventory、`--receiver` 与接收端 ID 一致、时钟正确、配对文件未过期 |
| 配对文件已存在或过期 | 保留已有接收端与 agent 配置，选择新的 `--out` 文件名重新生成配对文件，再仅执行 `agent-pair`；不要重跑整组初始化 |
| `agent --once` 退出码 2 | 检查 `delivery` 中的拒绝/不可达信息和目标 URL；不要通过重建数据库解决网络问题 |
| 设备显示过期、离线 | 检查 agent 是否持续运行、采样时间和网络；只运行一次采样不会保持在线 |
| Docker/GPU/更新状态未知 | 缺少工具、权限或更新状态文件时属正常结果；未知不等于安装失败或服务正常 |
| 同一 agent 配置正在使用 | 停止你自己的重复 agent 实例；不要删除锁文件来绕过运行中的进程 |

### 端口冲突

在接收端尚未启动时，例如改用 `18765`，同时调整端口、Host、Origin 和页面接收地址。下面脚本只用于本指南的单机回环配置；已部署 HTTPS 时应保留自己的域名配置。

```bash
export SHIKI_PORT=18765
python3 - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ['SHIKI_STATE_DIR']) / 'receiver/receiver.json'
config = json.loads(path.read_text())
port = int(os.environ['SHIKI_PORT'])
assert 1024 <= port <= 65535
assert config['listen']['host'] == '127.0.0.1'
assert config['receiver']['id'] == 'main' and len(config['receivers']) == 1
config['listen']['port'] = port
config['listen']['allowed_hosts'] = [f'127.0.0.1:{port}', f'localhost:{port}']
config['listen']['allowed_origins'] = [f'http://127.0.0.1:{port}', f'http://localhost:{port}']
config['receivers'][0]['url'] = f'http://127.0.0.1:{port}'
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY
export SHIKI_URL="http://127.0.0.1:$SHIKI_PORT"
```

终端 B 也需设置同一个 `SHIKI_URL`。如果 agent 已配对，在停止 agent 后只调整其 `receivers` 中相应目标的 URL，保留令牌及状态，不必重新注册。

### 更新、备份与边界

更新前记录当前源码提交，停止本项目服务，用受保护的本地备份完整保存运行数据目录，然后审查新版本及配置变化。备份包含凭据，需与原目录同等保护，不上传 GitHub；保留旧源码以便回退。不要对运行中的 SQLite 文件做不一致的手工复制，不要把旧数据库恢复进仍在发送新序号的 agent 环境。

新用户无需执行仓库里绑定维护者环境的 `deploy/workbench-config.json` 或 Docker root 快照安装示例。Docker 快照是独立的可选部署，需要先审查其中的用户组、路径和权限。

运行现有本地测试可用 `python3 -m unittest discover -s tests -v`。测试不替代自己设备上的真实采集、HTTPS 和手机访问验证。提交问题时提供版本、Python/系统版本和去敏错误，不提供私有运行数据或密钥。

本项目源码与随附文档采用 [MIT License](../LICENSE)。Python、systemd、Docker、Tailscale 等外部软件遵循各自许可证。
