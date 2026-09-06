# certbot-dnspod-hook

Small, standalone Certbot DNS-01 hooks for Tencent Cloud DNSPod.

为 Certbot 自动创建、验证和清理 DNSPod TXT 记录，支持 Snap 版 Certbot。
个人维护的精简工具，通过 GitHub Releases 分发；不提供托管服务或可用性承诺。

**0.3.0 / Alpha。** 已在一台实际服务器完成从零安装、正式续签与 nginx 部署，
并验证 0.3.0 升级、staging 续签及卸载预览；详见 [验证记录](docs/VALIDATION.md)。
首次使用会由 Certbot 执行 Let's Encrypt staging 测试，成功后才保存接入选项。

## 安装与接入

要求 Linux、Python 3.11+（含标准库 venv）、已有 Certbot 2.3+、openssl、systemd。
支持 Snap 的 `snap.certbot.renew.timer` 或系统包的 `certbot.timer`。
安装包自带 pip 和锁定的纯 Python 依赖，不要求系统 pip、ensurepip、uv 或 Node。
`setup` 接入已存在的证书；新证书申请见后文。

### 私有仓库安装

先用有仓库访问权的 GitHub 账号完成 `gh auth login`，下载固定版本：

```sh
gh release download v0.3.0 --repo Miniast/certbot-dnspod-hook --pattern install.sh --pattern '*-bundle.tar.gz' --dir /tmp/certbot-dnspod-hook-0.3.0
sudo sh /tmp/certbot-dnspod-hook-0.3.0/install.sh --bundle /tmp/certbot-dnspod-hook-0.3.0/certbot-dnspod-hook-0.3.0-bundle.tar.gz
```

GitHub 登录仅用于下载。安装和后续续期不需要 GitHub 凭证。
安装器验证脚本内固定的 SHA-256，并从包内离线安装所有依赖。

### 公开仓库的 curl 安装方式

仅在仓库和 Release 已公开后可使用；私有仓库请用上面的下载方式：

```sh
curl -fsSL https://github.com/Miniast/certbot-dnspod-hook/releases/download/v0.3.0/install.sh | sudo sh
```

安装位置为 `/opt/certbot-dnspod-hook/versions/<版本>-<安装编号>`，
固定入口为 `/usr/local/bin/certbot-dnspod-hook`，经 `current` 链接指向当前版本。
新版本校验和安装检查通过后才切换链接；失败保留旧版本。
运行同一安装流程可升级或重新安装，旧版本保留以便回退。
不会直接安装到系统 Python 的包目录，也不会更改证书或续期设置。

### 一条命令接入并续签

准备权限为 600 的凭证文件，例如 `dnspod.env`：

```dotenv
TENCENTCLOUD_SECRET_ID=YOUR_SECRET_ID
TENCENTCLOUD_SECRET_KEY=YOUR_SECRET_KEY
```

可选 `TENCENTCLOUD_TOKEN`。支持纯 `KEY=value`、成对单/双引号、空行和整行注释；
不执行 shell、不做变量替换，不支持 `export`、行尾注释或重复键。
文件需由 root 或执行 sudo 的用户所有。项目内 `.env`、`.env.*` 已加入 Git 忽略规则。

已有证书示例：

```sh
sudo certbot-dnspod-hook setup --cert-name example.com --zone example.com --credentials-file ./dnspod.env --renew-now --deploy-nginx
```

也支持行内指定凭证：

```sh
sudo certbot-dnspod-hook setup --cert-name example.com --zone example.com --secret-id 'YOUR_SECRET_ID' --secret-key 'YOUR_SECRET_KEY' --renew-now
```

行内凭证可能出现在 shell 历史和进程参数中；文件方式更适合长期使用。
程序不会把密钥加入 Certbot 命令或日志。

- `--cert-name`：`certbot certificates` 显示的证书名称。
- `--zone`：DNSPod 实际托管区域，可重复。检查证书中全部 DNS 名称都被配置区域覆盖。
- `--renew-now`：staging 成功后，执行一次正式强制续签；不带则只测试并关联。
- `--deploy-nginx`：先验证 nginx 配置，再保存“检查配置后 reload”的部署脚本。
  staging 也测试该部署钩子；若证书已有其他 deploy hook 则拒绝覆盖，省略此选项即可沿用原钩子。
- `--propagation-seconds` / `--propagation-timeout`：最低等待秒数和总超时，默认 120 / 600。

`setup` 备份原续期配置，将凭证复制到 root 所有、权限 600 的独立配置文件。
每次接入使用新的配置文件，避免失败时影响旧的凭证配置。
通过 `certbot reconfigure` 完成 staging 测试和 hooks 保存，然后启用已有的 Certbot timer。
Certbot 负责证书和续期配置；工具不直接重写 `/etc/letsencrypt/renewal/*.conf`。

失败时停止后续步骤：staging 失败不会触发正式续签；正式续签失败保留已通过测试的接入配置。
输出中会给出凭证配置、原配置备份和本地状态检查命令。
保留配置文件用于恢复可能未清理的挑战；后续续期不再依赖最初的凭证文件或终端环境。

## 验证接入与恢复

定时任务复用已保存的 hooks，可检查：

```sh
sudo certbot renew --cert-name example.com --dry-run
```

上面的 dry-run 默认不执行 deploy hook；若要一起检查 nginx reload，加 `--run-deploy-hooks`。
不要把 `--force-renewal` 加入日常定时任务；该选项仅用于明确要求的一次立即续签。

`setup` 打印实际配置路径。以下用 `CONFIG.toml` 代指它：

```sh
sudo certbot-dnspod-hook --config /etc/certbot-dnspod-hook/CONFIG.toml status
sudo certbot-dnspod-hook --config /etc/certbot-dnspod-hook/CONFIG.toml cleanup --state-id STATE_ID
```

`status` 只读本地状态，不联系 DNSPod。只有确认 Certbot 已停止使用该挑战时才手动清理。
若创建请求结果不明且列表暂时找不到记录，状态和配置保留；先核对 DNSPod，再定向恢复。
不要清空整个 `_acme-challenge`，同名 TXT 可能属于其他操作。

## 写入范围与卸载

工具自己的持久写入集中在以下位置：

| 位置 | 内容 |
| --- | --- |
| `/opt/certbot-dnspod-hook` | 独立程序环境、受管版本清单、当前版本链接和安装锁 |
| `/usr/local/bin/certbot-dnspod-hook` | 固定命令链接 |
| `/etc/certbot-dnspod-hook` | 凭证配置、接入前备份、文件归属清单、可选 nginx 钩子 |
| `/var/lib/certbot-dnspod-hook` | 挑战状态和进程锁 |

安装临时下载文件在 `/opt/certbot-dnspod-hook/.download-*` 内，正常结束或捕获到失败时清理。
安装禁用 pip 缓存，系统命令入口禁用 Python 字节码写入；不修改 shell 配置、工作目录或系统 Python 包。
升级保留登记过的旧程序环境，卸载时一并移除。
`status` 与卸载预览不创建目录、状态文件或锁文件。

Certbot 自身会更新 `/etc/letsencrypt`、工作目录和 `/var/log/letsencrypt`，
systemd / nginx 也会产生正常系统日志；这些属于证书管理和系统服务的预期写入，卸载不会删除。

先预览卸载范围：

```sh
sudo certbot-dnspod-hook uninstall --detach --dry-run
```

仅在确实准备停止使用这个工具时执行：

```sh
sudo certbot-dnspod-hook uninstall --detach
```

`--detach` 仅恢复本工具改变的续期选项，保留新签发的证书、私钥及 Certbot 的其他配置。
如果接入前使用手动 DNS 验证，恢复后需要另一套 hooks 才能继续自动续期。
省略 `--detach` 时，若任何证书仍引用本工具，则拒绝卸载。

卸载会核对受管配置的内容哈希以及 hooks 是否仍符合接入时记录；
有手动改动、未跟踪的引用、活动进程、待清理挑战或未知状态文件时会停止。
完整卸载移除登记过的程序环境、命令链接、凭证、备份和空状态目录。
配置目录里额外放入的用户文件会保留，不递归清空未知目录。
卸载用兼容 Certbot 的临时锁排除并发续期，结束后移除该临时锁。

## 范围与工作机制

- 使用腾讯云 API 3.0 和官方公共 SDK，支持 SecretId / SecretKey、可选临时 Token。
- 显式区域匹配，支持普通域名、子域名、泛域名和同名多值 TXT。
- 创建前写入私有本地状态，每笔操作具有独有的 Remark 标记。
- `CreateRecord` 不重发；创建结果不明时最多查询七轮、间隔 10 秒按标记查找。
- 清理按保存的记录 ID 查询，并核对名称、类型、值、Remark 后删除。
- 原子状态写入和本机进程锁；DNS 传播超时尝试清理，异常时保留可恢复状态。
- 最低传播等待时间后检查每个权威 NS，支持 UDP/TCP 回退。

当前不支持传统 DNSPod Token、国际站 API、CNAME 验证委托或跨机器共享状态。
权威 DNS 检查是本机采样，不保证 CA 的全球视角一致，默认仍保留 120 秒等待。
四个 API 的请求、完整响应、错误格式及字段映射见 [接口文档](docs/DNSPOD_API.md)。
所需操作权限为 `CreateRecord`、`DescribeRecordList`、`DescribeRecord`、`DeleteRecord`。

## 手动配置与新证书

安装器只安装程序，不要求先执行 `setup`。也可以手动准备权限 600 的 TOML 配置：

```toml
secret_id = "YOUR_SECRET_ID"
secret_key = "YOUR_SECRET_KEY"
zones = ["example.com"]
state_dir = "/var/lib/certbot-dnspod-hook/example.com"
propagation_seconds = 120
propagation_timeout = 600
ttl = 600
```

完整选项见 [示例配置](examples/config.example.toml)。配置和状态要求由执行用户所有，
权限分别为 600 和 700。全局默认配置路径仍为 `/etc/certbot-dnspod-hook/config.toml`。
环境变量 `TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY`、`TENCENTCLOUD_TOKEN`
优先于 TOML；普通 auth/cleanup 不自动读取 `.env`，`setup --credentials-file` 负责导入。

首次申请新证书，指定准备好的配置路径、域名和邮箱，先测试：

```sh
sudo certbot certonly --dry-run --non-interactive --agree-tos --email admin@example.com \
  --manual --preferred-challenges dns \
  --manual-auth-hook '/usr/local/bin/certbot-dnspod-hook --config /etc/certbot-dnspod-hook/config.toml auth' \
  --manual-cleanup-hook '/usr/local/bin/certbot-dnspod-hook --config /etc/certbot-dnspod-hook/config.toml cleanup' \
  -d example.com -d '*.example.com'
```

成功后移除 `--dry-run` 正式申请。新证书需要明确域名和 ACME 条款同意，因此不由 `setup` 猜测创建。

## 开发与发布

```sh
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
uv run --frozen python scripts/build_release.py --repository Miniast/certbot-dnspod-hook
DNSPOD_TEST_RELEASE="$PWD/dist/release/0.3.0" uv run --frozen pytest -q tests/test_installer.py
```

构建器从 `uv.lock` 选择运行时依赖和 pip 的通用 wheel，下载后校验锁定的哈希，
再打包应用 wheel、依赖、带哈希的 requirements 和许可证。
产物位于 `dist/release/0.3.0`：`install.sh`、`*-bundle.tar.gz`、`SHA256SUMS`、
`requirements.txt`、`release.json`。安装器内固定 bundle 哈希，校验文件也列出安装器自身的哈希。
这些校验用于检测内容不符；安装器本身仍需从你信任的发布来源取得。

不传 `--repository` 时生成仅供本地测试的发布包，安装器需给 `--base-url` 或 `--bundle`。
测试 HTTP 仅允许 localhost / 回环地址；其他下载地址要求 HTTPS。
安装器的 `--prefix`、`--bin-dir` 可用于临时目录中的独立安装验证。

发布到私有仓库时，将上述产物附加到对应版本的 GitHub Release，并标记 prerelease。
不要把 `.env`、凭证、状态、证书或本地验证日志提交到 Git。
MIT 许可证见 [LICENSE](LICENSE)。

## 官方参考

- [Certbot 自动续期与修改续期配置](https://eff-certbot.readthedocs.io/en/stable/using.html#modifying-the-renewal-configuration-of-existing-certificates)
- [腾讯云官方 Python SDK](https://github.com/TencentCloud/tencentcloud-sdk-python)
- [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
