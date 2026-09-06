# 0.4.0 单目录与实际卸载验证

日期：2026-09-07（Asia/Shanghai）。源码版本：`f7ed743a3bce2b18c78759f2977355d834ec4cb0`。
环境为 Ubuntu 24.04.4、Python 3.12.3、Snap Certbot 5.8.0、systemd 和 nginx。

## 实际流程

1. 对已有证书、私钥、nginx 配置计算哈希，记录原续期选项、timer 状态和 DNS 挑战记录。
2. 用 0.3.0 的 `uninstall --detach` 解除旧关联，移除原来的四处安装路径。
3. 使用 0.4.0 bundle 实际安装，默认目录为 `/root/.certbot-dnspod-hook`。
   顶层仅有 `bin`、`current`、`versions`、`config`、`state`、安装清单和安装锁。
   命令链接也在目录内，没有全局命令链接。
4. 用 SecretId / SecretKey 文件执行 setup，验证 `miniast.tech` 和 `*.miniast.tech`。
   两条真实 staging 挑战成功，临时 TXT 清理成功，nginx 部署钩子成功。
   本次未再次强制签发生产证书。
5. 对安装目录、续期配置和 Certbot 锁文件执行前后快照比较：
   `status` 与 `uninstall --detach --dry-run` 未改变文件内容、权限或修改时间。
6. 执行 0.4.0 的 `uninstall --detach`，实际恢复续期选项并移除整个安装目录。

## 最终核对

| 项目 | 结果 |
| --- | --- |
| 新版 `/root/.certbot-dnspod-hook` | 已删除 |
| 旧版 `/opt/certbot-dnspod-hook` | 已删除 |
| 旧版 `/etc/certbot-dnspod-hook` | 已删除 |
| 旧版 `/var/lib/certbot-dnspod-hook` | 已删除 |
| 旧版 `/usr/local/bin/certbot-dnspod-hook` | 已删除 |
| Certbot 工具引用 | 已移除，受管字段与最初接入前一致 |
| 证书和私钥 | 文件集合及内容哈希均与本次改造前一致 |
| nginx 配置 | 文件集合及内容哈希均未变，配置检查通过，服务 active |
| 本机 TLS | 127.0.0.1:443 / SNI miniast.tech 返回保留的证书 |
| 保留证书到期时间 | 2026-12-05 15:06:30 UTC，即北京时间 23:06:30 |
| 原有 Certbot timer | enabled / active，与改造前一致 |
| DNS 挑战记录 | 与本次测试前完全一致，原有 TXT 保留 |
| 待清理挑战和临时 Certbot 锁 | 无 |

源码项目、用户提供的原始凭证文件和构建产物保留；部署目录内的凭证副本已随卸载删除。
Certbot 的正常日志和证书归其自身管理，保留。本机已解除本工具的自动 DNS 验证关联；
已有证书继续可用，后续自动续签需要重新接入本工具或其他验证方式。

## 自动化验证

- 本地 **76 项测试通过**，包含 HTTP 安装、损坏包回退、实际自行卸载和单目录位置检查。
- 保留配置改动、待清理挑战、未知状态文件、用户额外文件及续期选项恢复的保护测试。
- 新增检查：未启用的 timer 不会被 setup 修改，尚未解除的旧版 hooks 会阻止迁移。
- Ruff、格式检查、安装脚本语法检查及 Git 空白检查通过。
- [GitHub CI](https://github.com/Miniast/certbot-dnspod-hook/actions/runs/34065446548)通过，
  包含 Python 3.11–3.14 和安装器集成检查。
- 发布前检查源码和 bundle 内全部 wheel，未包含本机实际双密钥，`.env` 被 Git 忽略。

## 为什么需要先解除关联

工具自身的文件可以集中在一个目录；系统 Certbot 的证书配置由 Certbot 管理，
其 timer 运行时需要从那里读取 hooks。删除工具目录不会触发恢复代码，
因此完成 setup 后不能直接删目录代替卸载。
`uninstall --detach` 先恢复外部续期选项，再清理本目录，且保留已经签发的证书。
