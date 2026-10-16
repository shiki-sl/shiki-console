# Docker 监控及服务入口接续

以下记录截至 2026-10-16 18:40（Asia/Shanghai），继续操作前须核对实时状态。

## 已完成

- 代码：`/home/shiki/code/shiki-console`，主分支 `main`。3600 的同路径是部署副本，没有 `.git`，不能在远端执行 Git 更新。
- 两台机器已安装独立的 `shiki-docker-snapshot.service/timer`。root helper 只执行固定只读查询，用户 agent 仅读取 `/run/shiki-docker/status.json`。未将用户加入 docker 组，未修改 socket 权限。
- 9955HX 的真实快照已进入 HTTPS dashboard，`open-webui` 为 running / healthy，CPU、内存有实际值；手机尺寸浏览器已确认容器详情显示。
- Open WebUI 本地端口 3000 的健康检查通过。新增仅 Tailnet 的 HTTPS 8443 → `http://127.0.0.1:3000`，SHIKI“应用”页已有入口。由 3600 通过此 HTTPS 地址检查 `/health` 得到 200，TLS 验证通过。未开放公网或使用 Funnel。
- 原 SHIKI HTTPS 443 → 8765 保持运行。手机此前已安装 PWA；本次未再操作真实手机。
- 14 项测试通过；随后为适配无额外 capability 的 systemd 环境，将 `/proc/PID/exe` 改为 `/proc/PID/comm`，定向复跑新增 3 项测试通过。Astra high 定点权限审查已通过，实际 helper 在两台机器均成功写出受限快照。

## 3600 启动阻塞

用户已明确允许启动 3600 的 Docker。已执行一次 `systemctl start docker.service`；其自身 Restart 策略重试三次后进入 failed/start-limit-hit。未重复手动启动，未改 Docker 配置。

启动日志：`failed to create NAT chain DOCKER`，`iptables v1.8.13 (nf_tables): Could not fetch rule set generation id: Invalid argument`。

当前运行内核 `7.2.7-arch1-1`，磁盘模块目录为 `7.2.9-arch1-1` 与 `6.18.56-1-lts`。`modprobe -n -v nf_tables` 明确报告当前内核目录下找不到模块。没有切换 iptables 实现、禁用防火墙或重启机器来绕过。

**整机重启尚未得到用户确认。** 当前聊天已发出询问；若没有新的明确许可，停在重启前。用户允许启动 Docker，不代表允许中断整机的现有会话。

只读检查实际 Docker 数据目录 `/var/lib/docker` 发现：

- `biz-redis`：原状态停止，restart policy 为 `unless-stopped`，端口仅绑定 `127.0.0.1:6379`。
- `biz-nacos`：原状态停止，restart policy 为 `no`，端口仅绑定回环地址。

系统及用户服务、进程、常用监听端口、上述容器清单及包查询均未发现 MySQL / Nginx。不要为填充监控画面擅自安装、重建或启动数据库；应让用户确认真实部署对象和位置。Docker 恢复后，已配置采集会自动展示能读取的容器状态；不要把 Docker daemon 正常当成 Redis、Nacos 业务验收通过。

## 可复制的接续提示词

> 继续完成 shiki-console 的 3600 Docker 恢复与手机服务监控。先读取本项目 README、HANDOFF.md 与当前生效 AGENTS 规则。9955HX 的 Docker 快照和 Open WebUI Tailnet HTTPS 8443 已验证；3600 的监控桥接已装好，但 Docker 因旧运行内核缺失 nf_tables 模块而启动失败。已有授权允许启动 3600 Docker，以及本任务代码提交推送；整机重启仍须核对当前聊天是否有新的明确批准，不能从此文推断已批准。若获准重启，先核验重启影响和 SSH/Tailscale/agent 自动恢复条件，再在恢复后确认运行内核和模块匹配、启动 Docker，并按真实容器状态核对 SHIKI dashboard。保留业务容器及数据，不重建、不删库，不强制启动用户原本停止的容器。MySQL/Nginx 尚未发现，先向用户核对实际部署位置，再作有界只读检查。复用现有测试及证据，修复和直接影响行为验证充分后提交并普通 push 到已核实的 origin/main。报告真实手机验收与浏览器/API 验证的差别。

## 恢复与回退位置

- 两机 agent 原配置备份：`runtime/20261016-docker-bridge/agent-backup/agent.json`；3600 同目录另存原 `agent.py`、`collector.py`。备份含配对凭据，不得提交或打印。
- 9955HX 接收端配置备份：上述备份目录中的 `receiver.json`。
- 停用快照服务：移除 agent 的 `docker_snapshot_file` / `docker_snapshot_max_age_seconds` 配置后重启用户 agent，再停用专用 timer。无 socket 权限时恢复为 unknown。
- 移除新 Open WebUI HTTPS 入口仅使用 `tailscale serve --https=8443 off`；不得 reset 全部 Serve 配置，否则会影响 SHIKI 443。
