# certbot-dnspod-hook

Small, standalone Certbot DNS-01 hooks for Tencent Cloud DNSPod.

用一个小型 Python 工具，为 Certbot 自动创建、验证和清理 DNSPod TXT 记录。
通过 Certbot 官方 hook 接口接入，独立于 Certbot 的 Python 环境，可配合 Snap 版使用。

**状态：0.1.0 / Alpha。** 已提供离线测试；尚未完成真实 DNSPod + Let's Encrypt staging 联调。
生产接入前，请在自己的域名上完成下面的测试续期。项目尚未发布到 PyPI。

## 范围

- Linux、Python 3.11+；两个直接依赖：腾讯云官方 SDK 公共包、dnspython。
- 腾讯云 API 3.0，使用 SecretId / SecretKey，可附带临时凭证 Token。
- 多个显式配置的 DNSPod 托管区域；普通域名、子域名和泛域名。
- 同名多值 TXT、重复调用、独立清理；校验记录 ID、名称、类型、值和操作标记。
- 最低传播等待时间 + 查询每个权威 NS，带超时和 UDP/TCP 回退。
- 私有本地状态、原子写入和进程锁，支持中断后的定向清理。

第一版不支持传统 DNSPod Token、国际站 API、CNAME 验证委托或跨机器共享状态。
证书签发、私钥、调度和部署交给 Certbot。该程序不会自行修改 nginx 或安装计时器。

## 安装

开发环境，在项目根目录执行：

~~~sh
uv sync --frozen
uv run --frozen certbot-dnspod-hook --help
uv run --frozen pytest -q
~~~

服务器部署使用固定路径的独立环境。以下命令由管理员执行，要求系统 Python 3.11+
且支持 venv；Ubuntu 可能需要先安装对应的 python3-venv 包。

~~~sh
uv build
uv export --frozen --no-dev --no-emit-project --format requirements-txt \
  --output-file dist/requirements.txt
sudo /usr/bin/python3 -m venv /opt/certbot-dnspod-hook
sudo /opt/certbot-dnspod-hook/bin/pip install \
  -r dist/requirements.txt dist/certbot_dnspod_hook-0.1.0-py3-none-any.whl
~~~

发布包和锁定的依赖在部署时安装。定时续期直接调用已安装的入口，
不会运行 uv、临时安装依赖或依赖交互式 shell 的 PATH。
以 root 运行 Certbot 时，工具环境、配置及其父目录应由 root 管理。

## 配置

~~~sh
sudo install -d -m 700 /etc/certbot-dnspod-hook /var/lib/certbot-dnspod-hook
sudo install -m 600 examples/config.example.toml /etc/certbot-dnspod-hook/config.toml
sudoedit /etc/certbot-dnspod-hook/config.toml
~~~

填写实际凭证及托管区域，例如：

~~~toml
secret_id = "YOUR_SECRET_ID"
secret_key = "YOUR_SECRET_KEY"
zones = ["example.com", "example.net"]
state_dir = "/var/lib/certbot-dnspod-hook"
propagation_seconds = 120
propagation_timeout = 600
ttl = 600
~~~

配置文件要求为当前执行用户所有、权限 600；状态目录权限 700。
所有参数示例见 [config.example.toml](examples/config.example.toml)。

也可以通过环境变量传入凭证：
TENCENTCLOUD_SECRET_ID、TENCENTCLOUD_SECRET_KEY、TENCENTCLOUD_TOKEN；
它们优先于配置文件。定时任务不会自动继承终端里 export 的变量。

zones 填写 DNSPod 实际托管区域。例如 www.example.com 的证书通常只需配置 example.com；
若 sub.example.com 本身是独立托管区域，则将它单独加入，程序选择最长的域名边界匹配。
不通过账户列表猜测域名，也不简单截取最后两个标签。

API 需要 CreateRecord、DescribeRecordList、DescribeRecord、DeleteRecord 四项操作权限。
使用专用凭证，并在腾讯云访问管理中按实际支持的资源粒度限制到目标域名。
日志仅记录错误码和请求 ID，不输出 SDK 错误正文或凭证。
不要将真实配置、证书、私钥或状态文件提交到 Git。

## 接入 Certbot

### 已有证书

Certbot 2.3+ 可通过 reconfigure 测试并保存新的续期选项。先确认实际证书名称：

~~~sh
sudo certbot certificates
sudo certbot reconfigure --cert-name example.com \
  --authenticator manual --preferred-challenges dns \
  --manual-auth-hook "/opt/certbot-dnspod-hook/bin/certbot-dnspod-hook auth" \
  --manual-cleanup-hook "/opt/certbot-dnspod-hook/bin/certbot-dnspod-hook cleanup"
~~~

reconfigure 会联系 Let's Encrypt staging，并真实创建、清理 DNS TXT；
成功后保存续期选项，但不会替换现有生产证书。然后验证已保存的配置：

~~~sh
sudo certbot renew --cert-name example.com --dry-run
~~~

若证书已到续期时间，执行一次正式续期：

~~~sh
sudo certbot renew --cert-name example.com
~~~

### 新证书

先测试域名验证，替换实际域名与联系邮箱：

~~~sh
sudo certbot certonly --dry-run --non-interactive --agree-tos \
  --email admin@example.com \
  --manual --preferred-challenges dns \
  --manual-auth-hook "/opt/certbot-dnspod-hook/bin/certbot-dnspod-hook auth" \
  --manual-cleanup-hook "/opt/certbot-dnspod-hook/bin/certbot-dnspod-hook cleanup" \
  -d example.com -d '*.example.com'
~~~

确认成功后，移除 --dry-run 再申请生产证书。
后续使用 certbot renew，Certbot 会复用保存的 hook 配置。

### 调度和 nginx

沿用安装方式已有的续期任务。Snap 版通常使用 snap.certbot.renew.timer；
检查是否启用，避免重复添加 cron。

nginx 可选部署钩子：

~~~sh
sudo install -m 755 examples/nginx-deploy.sh \
  /etc/letsencrypt/renewal-hooks/deploy/20-nginx-reload
~~~

它先检查 nginx 配置，再 reload。deploy hook 仅在成功签发/续期后执行；
dry-run 默认不运行它。需要测试部署钩子时，另加 --run-deploy-hooks，
这会实际调用 nginx 检查和 reload，应在确认配置正确后执行。
同时监测续期失败、部署钩子日志与站点实际证书有效期；计时器处于 enabled 不代表续期成功。

## 工作方式与恢复

auth 从 CERTBOT_IDENTIFIER（兼容 CERTBOT_DOMAIN）和 CERTBOT_VALIDATION 获取挑战；
stdout 仅输出一个状态 ID。cleanup 验证 CERTBOT_AUTH_OUTPUT 与当前挑战相符；
认证脚本失败而没有 stdout 时，仍可从同一组挑战变量定位状态。

创建 TXT 前，先保存唯一操作标记，并将标记写入 DNSPod 的 Remark。
创建请求只发送一次。网络超时后，按标记轮询列表至多七次，间隔 10 秒，
覆盖 DNSPod 文档指出的约 30 秒索引延迟。
如果仍不确定结果，保留状态并报错，不再次创建或接管同值的外部记录。
对于明确的权限、参数或限流拒绝，丢弃未完成状态，以便修正配置后重试。

cleanup 按记录 ID 查询，核对类型、主机名、值和标记后才删除。
记录被别人修改时拒绝删除；API 失败时保留状态。DNS 传播超时也会尝试清理。
SIGKILL、断电或任意时刻的进程退出可能留下状态，可在确认 Certbot 已停止后处理：

~~~sh
sudo /opt/certbot-dnspod-hook/bin/certbot-dnspod-hook status
sudo /opt/certbot-dnspod-hook/bin/certbot-dnspod-hook cleanup --state-id STATE_ID
~~~

status 只读本地状态，不联系 DNSPod；--state-id 仅清理指定挑战。
不要在另一笔续期仍使用该 TXT 时手动清理。
所有本机调用应共用一个状态目录；锁只覆盖 hook 执行，不协调整笔 ACME 订单或其他机器。

如果待定记录一直找不到，先在 DNSPod 控制台核对对应主机名、TXT 值和 Remark。
只有确认那次创建未发生、没有待清理记录后，才手动移除该 ID 对应的本地 JSON，
再重新验证。程序刻意不提供“清空全部 TXT”操作。

权威 DNS 检查是本机采样，不保证 Let's Encrypt 的全球视角完全一致；
DNS anycast、缓存和委托都可能造成差异。默认等待 120 秒仍失败时，
结合实际传播情况调整 propagation_seconds 和更大的 propagation_timeout。
TTL 需符合 DNSPod 套餐限制，它不等于传播等待时间。

## 开发与发布

~~~sh
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
uv build
~~~

测试使用模拟 API 和 DNS 响应，不需要凭证，也不会修改真实 DNS。
GitHub Actions 已配置 Python 3.11–3.14 的测试矩阵。
源码位于 src/certbot_dnspod_hook，按配置、API、状态、验证流程、DNS 查询和命令入口分开。

推送前选择 GitHub 账号/组织和公开或私有仓库，创建名为 certbot-dnspod-hook 的空仓库，
不在 GitHub 端初始化 README，然后执行：

~~~sh
git remote add origin git@github.com:YOUR_ACCOUNT/certbot-dnspod-hook.git
git push -u origin main
~~~

GitHub 简介建议：Small Certbot DNS-01 hooks for Tencent Cloud DNSPod.
建议 topics：certbot、dnspod、dns-01、letsencrypt、python。

首个版本发布前完成真实 staging 验证、TXT 清理与部署钩子验证，
再标记版本。MIT 许可证见 [LICENSE](LICENSE)。

## 官方接口参考

- [Certbot hooks 与自动续期](https://eff-certbot.readthedocs.io/en/stable/using.html#pre-and-post-validation-hooks)
- [Certbot 修改续期配置](https://eff-certbot.readthedocs.io/en/stable/using.html#modifying-the-renewal-configuration-of-existing-certificates)
- [DNSPod CreateRecord](https://cloud.tencent.com/document/api/1427/56180)
- [DNSPod DescribeRecordList](https://cloud.tencent.com/document/api/1427/56166)
- [DNSPod DescribeRecord](https://cloud.tencent.com/document/api/1427/56168)
- [DNSPod DeleteRecord](https://cloud.tencent.com/document/api/1427/56176)
- [腾讯云官方 Python SDK](https://github.com/TencentCloud/tencentcloud-sdk-python)
- [Let's Encrypt DNS-01 与传播限制](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge)
