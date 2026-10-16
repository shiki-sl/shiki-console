# SHIKI · 个人计算环境控制台

面向 Android 手机的只读 Web 控制台。设备主动注册、独立向多个接收端上报；手机查看汇总状态并打开已配置的 Web 应用。页面不提供关机、重启、容器操作、终端、更新触发或配置修改接口。

## 当前设备与故障范围

| 设备 | 位置 | 角色 | 当前接入范围 |
| --- | --- | --- | --- |
| 9955HX | 办公室 | 本机、主接收端、采集端 | 已部署，Tailnet HTTPS |
| 3600 | 办公室 | 采集端 | 已部署，HTTPS 持续上报 |
| cpu / 12400 | 异地 | 未来备用接收端、采集端 | 待部署 |
| sd | 可移动 | 可选采集端 | 待部署；不参与更新管理 |

9955HX 与 3600 共享办公室故障范围，不能用二者互备来声称已具备异地容灾。12400 有独立物理位置，但仍须实际验证网络、电源和跨网络访问。sd 不承担固定接收端职责。

设备名、位置、地址、服务、阈值及应用入口均由配置决定；硬件不可读取时显示 N/A，不生成演示指标。

## 运行方式

仅依赖 Python 3.11+ 标准库，前端是无构建依赖的 HTML/CSS/JavaScript。源码目录为 `/home/shiki/code/shiki-console`，当前分支 `main`。本次本机运行数据位于已忽略的 `runtime/`；长期运行建议放在源码外。不提交或分享 viewer key、注册码、设备 token、数据库或日志。

当前部署已由用户级 systemd 服务运行，并已启用开机启动（两台机器的用户 lingering 均已开启）：

```bash
# 9955HX：接收端与本机采集端
systemctl --user status shiki-receiver shiki-agent
# 3600：仅采集端
ssh shiki@172.19.8.7 'systemctl --user status shiki-agent'
```

以下命令用于手动调试，仅在对应 systemd 服务已停止时使用：

```bash
cd /home/shiki/code/shiki-console
python3 -m shiki serve --data-dir runtime/receiver
python3 -m shiki agent --config runtime/agent.json
```

以下为新设备的初始化示例，请先进入源码目录：

```bash
python3 -m shiki init --data-dir /absolute/path/shiki-runtime/receiver
python3 -m shiki serve --data-dir /absolute/path/shiki-runtime/receiver
```

默认只监听 `127.0.0.1:8765`，用于本机验证。浏览器打开该地址并使用 `viewer.key` 中的查看密钥登录。该密钥只允许读取监控快照，不能注册设备或执行设备操作。

在另一个终端初始化和配对本机 agent：

```bash
python3 -m shiki agent-init --config /absolute/path/shiki-runtime/agent.json --device 9955hx
python3 -m shiki pair --data-dir /absolute/path/shiki-runtime/receiver --device 9955hx --out /absolute/path/shiki-runtime/pair-code
python3 -m shiki agent-pair --config /absolute/path/shiki-runtime/agent.json --url http://127.0.0.1:8765 --receiver main --code-file /absolute/path/shiki-runtime/pair-code
python3 -m shiki agent --config /absolute/path/shiki-runtime/agent.json
```

注册码由接收端本地 CLI 生成，绑定设备并限时、一次使用。每个接收端单独配对。请通过已有可信渠道传递配对文件，不把注册码放进 URL、公开日志或仓库。

## 手机访问与未来备用接收端

当前手机入口：`https://shiki-9955hx.tailaed862.ts.net/`。安卓手机须连接同一 Tailnet，在 Chrome 打开并输入本机 `runtime/receiver/viewer.key` 中的查看密钥；不要在聊天、URL 或仓库里分享密钥。Chrome 菜单选择“添加到主屏幕 / 安装应用”，安装后从桌面图标打开。查看密钥仍是会话级存储，重新打开会话时可能需要再次输入。

本机 Tailscale Serve 在私网 443 端口提供自动续期的可信 HTTPS，反向代理到 `127.0.0.1:8765`。没有启用 Funnel。3600 已独立配对并通过同一 HTTPS 地址上报；两端设备 token 各自保存在私有运行目录。

两台电脑使用 `--accept-dns=false --accept-routes=false`，保留原有 DNS 和路由策略。为解析接收端域名，各自 `/etc/hosts` 仅增加 `100.93.232.86 shiki-9955hx.tailaed862.ts.net`，修改前备份为 `/etc/hosts.shiki-console-20261016.bak`。若将来重新注册导致接收端地址变化，应核对并更新此单条映射。

接收端 Host/Origin 配置的原始备份为 `runtime/deploy-backup-20261016/receiver-before-tailnet.json`。临时关闭手机入口可执行 `tailscale serve --https=443 off`；暂停采集使用对应机器的 `systemctl --user stop shiki-agent`。这些操作不需要修改既有更新 timer 或其他应用服务。

`127.0.0.1` 是执行设备自身的地址，手机不能用它连接电脑。正式手机接入需要一个手机可达、证书可信的 HTTPS 地址；允许局域网或已有 Tailnet 路径。本项目不会自动开放防火墙、安装 Tailscale、建立公网映射或修改路由。

接收端支持直接 TLS；也可在明确部署授权后使用已有可信 HTTPS 反向代理，后端保持 loopback。具体证书、监听地址、Host 与浏览器 Origin 必须匹配配置，不能为了连通关闭 TLS 验证或开放任意跨域来源。

12400 备用部署流程：

1. 复制相同源码，在 12400 初始化独立运行目录和接收端身份；复制并核对非敏感设备清单，**不要复制主端的凭据或整个数据库**。
2. 为手机建立可信 HTTPS 访问，两个接收端分别允许所需的前端 Origin。
3. 在备用接收端为每个设备生成配对码，让 agent 增加该接收目标；原有主端目标保留。
4. 在手机设置中添加备用地址及其独立查看密钥，配置顺序由主到备。
5. 验证主端故障后备用仍收到新采样、页面可切换、主端恢复后不会用旧快照覆盖新值。

备用端标注“待部署”时不会当成离线报警，但也不代表已具备备用能力。跨设备、跨站点容灾须以实际部署和故障演练为准。当前仅实现及本机验证协议与切换逻辑。

两个接收端都保存最新快照并提供前端。已安装的 Android PWA 可以利用静态离线缓存打开页面；首次打开仍需要一个可用入口。浏览器后台不会保证固定周期刷新，持续采集由 agent 负责。

## Web 应用入口

应用目录是可信配置中的链接集合，例如 Grafana 或个人 Web 工具。每个入口包含名称、完整 HTTP/HTTPS 地址、说明及访问范围（局域网 / Tailnet / 公网）。点击在新页面打开，不嵌入 iframe、不代理任意 URL、不自动扫描设备端口。

**设备能上报不等于手机能访问它的 Web 端口。** 应用须监听合适的接口，手机须有相应网络路径与应用自身的访问权限。应用目录不会绕过应用登录，也不会自动改变绑定地址、防火墙或访问权限。没有配置真实地址时，不生成猜测的入口。

在接收端运行目录的 `receiver.json` 中找到对应 `inventory` 设备，把入口加入它的 `apps` 数组，再重启本项目接收端。下面仅说明格式，地址必须替换为实际可达地址：

```json
{
  "id": "grafana",
  "name": "Grafana",
  "url": "https://grafana.example.com",
  "description": "个人监控面板",
  "access": "tailnet",
  "enabled": true
}
```

`access` 可选 `lan`、`tailnet`、`public`，只是访问范围提示，不会改变网络权限。备用端应单独维护相同的非敏感目录。接收端的 `listen.allowed_hosts` 填精确主机名与端口；`listen.allowed_origins` 填允许连接本端的前端来源，两个接收端的来源应互相配置。CSP 允许的接收地址取自 `receivers[].url`，因此只在手机端新增备用地址还不足以允许跨域访问。

## 指标与权限

- CPU、内存、磁盘：读取 Linux `/proc`、文件系统统计与 `lm-sensors`。
- GPU：调用已有 `nvidia-smi` 或读取可用的 AMD sysfs；驱动异常、不支持或权限不足时保留原因。
- Docker：仅在可安全读取时获取 daemon、容器状态和资源占用。服务或 socket 未运行时不主动触发启动；不会添加 docker 组、sudo 或向 Web 暴露 Docker socket。
- Tailscale、Sunshine：读取既有状态，不安装或修改网络。服务未发现与应用端到端可用是不同结论。
- 更新：读取既有结果记录及状态，区分成功、失败、延后和未知；不执行任何更新检查或更新程序。重启需求缺少可靠依据时保持未知。
- Codex：只读进程及可用的会话元数据；模型和任务标注为最近观测值，不将进程存活当成任务成功。token/s 在没有可靠计时来源时为 N/A。不读取或上报认证 token、提示词正文、工具输出或完整日志。

agent 默认读取 `~/.local/state/codex-cli-update/status.json`、`~/.local/state/codex-gui-update/status.json` 与 `/var/lib/official-package-update/state.json`。可通过 `update_status_files` 分别指定 `cli`、`gui`、`system` 路径，读取失败显示未知。默认更新记录时效为 48 小时，普通设备数据为 90 秒；两者独立。`codex_metadata` 默认关闭，本机实例已开启精确字段读取。`known_health_urls` 可配置 `grafana`、`loki` 的真实只读健康地址，未配置不扫描端口。

## 健康状态与数据时效

状态同时区分：设备/服务异常、不可达、未注册、指标未知及数据过期。可选设备离线和未安装的可选服务不等同于故障。只有必需项齐全且新鲜、没有异常时，才显示 `ALL SYSTEMS OPERATIONAL`。

接收端故障或手机断网时显示最后成功时间和过期提示，保留的数字不当成实时数据。备用切换会显示降级提示；两端都不可用时明确显示实时监控不可用。注册端仅保留最新快照，agent 重试上报最新值，第一版不提供历史曲线或长时间离线日志补传。

## 边界

当前已按部署授权新增本项目用户级 systemd 服务及 Tailscale 服务；既有更新 timer、软件源和其他应用服务保持原状。12400 备用端尚未部署。

公网暴露、无认证访问、自动信任新设备、任意命令执行、Docker 透传和任意 URL 代理均不在本项目第一阶段范围内。

本机验收记录独立保存在 `/home/shiki/文档/code/shiki-console/VALIDATION.md`，不随源码继续维护。
