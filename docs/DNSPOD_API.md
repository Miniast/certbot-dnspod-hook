# DNSPod 接口清单与请求／响应格式

对应当前 `certbot-dnspod-hook` 0.4.0 实现，核对日期：2026-09-06。
API 适配代码见 [provider.py](../src/certbot_dnspod_hook/provider.py)，
调用流程见 [core.py](../src/certbot_dnspod_hook/core.py)。

本文列出项目实际发送的全部 DNSPod API 请求参数，以及成功响应和内部返回格式。
成功响应的字段名及 JSON 类型已与本次真实 TXT 创建、查询和删除的返回核对；
示例中的域名、记录 ID、请求 ID、时间、挑战值等均为替换值，不是原始日志。
未使用字段也列出，以便区分原始 API 响应和项目内部数据。
腾讯云可能增加字段；本文不保证未使用字段在所有记录类型、套餐下均非空。

## 1. 总览与调用时机

| API Action | 项目方法 | 用途 | 方法返回值 |
| --- | --- | --- | --- |
| `DescribeRecordList` | `DNSPod.records(zone, name)` | 创建前检查同名记录；创建结果不明时按标记寻找记录 | `list[dict]` |
| `CreateRecord` | `DNSPod.create(state, ttl)` | 创建一条带独有 Remark 的 TXT | 正整数记录 ID |
| `DescribeRecord` | `DNSPod.get(zone, record_id)` | 恢复已有挑战、清理前核对记录 | 统一格式的 `dict`；记录不存在时 `None` |
| `DeleteRecord` | `DNSPod.delete(zone, record_id)` | 删除已核对的记录 | `None`；失败抛异常 |

一次新挑战的正常流程：

1. `DescribeRecordList` 检查目标名称是否有 CNAME 或无归属的同值 TXT。
2. 先保存本地操作状态，再 `CreateRecord`，保存返回的记录 ID。
3. 通过 DNS 协议等待 TXT 在权威服务器可见；auth 返回本地状态 ID。
4. Certbot 完成验证后调用 cleanup；`DescribeRecord` 按保存的 ID 读取记录。
5. 核对名称、类型、TXT 值和 Remark，全部相符才 `DeleteRecord`，然后移除本地状态。

重复调用或异常恢复可能改变调用次数；并非每次 auth 都创建记录。
配置中的 `zones` 显式指定托管区域，因此没有域名列表或域名发现 API。
当前不调用 `DescribeDomainList`、`DescribeDomain`、`DescribeRecordLineList`、
`CreateTXTRecord`、修改记录或其他 DNSPod API。`status` 只读取本地文件。

## 2. 四个 API 共用的请求格式

| 项目 | 当前使用值 |
| --- | --- |
| 地址 | `https://dnspod.tencentcloudapi.com/` |
| 方法／请求体 | HTTPS POST，JSON 对象 |
| `Content-Type` | `application/json` |
| `X-TC-Action` | 上表中的 Action |
| `X-TC-Version` | `2021-03-23` |
| `X-TC-Timestamp` | SDK 生成的 Unix 秒级时间戳 |
| `Authorization` | SDK 生成的 `TC3-HMAC-SHA256` 签名 |
| `X-TC-Token` | 仅使用临时凭证 Token 时传入 |
| Region | 这些接口无需地域，当前不发送地域值 |
| SDK HTTP 超时配置 | `reqTimeout=20` 秒 |

签名、公共请求头和 JSON 序列化由腾讯云官方 `CommonClient` 完成。
SecretId 用于标识凭证；SecretKey 在本地参与签名，不作为业务请求参数发送。
下文的“请求体”就是项目传给 `client.call_json(action, params)` 的 `params`。

成功时，SDK 返回完整的 `{"Response": {...}}`；项目的 `DNSPod.call()`
提取里面的 `Response`，再由 `records/create/get/delete` 分别处理。
不要把下面三层混为一谈：**完整响应 → Response 内容 → 项目方法返回值**。

## 3. DescribeRecordList：查询同名记录

官方接口：[获取域名的解析记录列表](https://cloud.tencent.com/document/product/1427/56166)。

当前请求体：

```json
{
  "Domain": "example.com",
  "SubDomain": "_acme-challenge.www",
  "Offset": 0,
  "Limit": 100,
  "ErrorOnEmpty": "no"
}
```

- `Domain`（string）：DNSPod 托管区域，从配置的 `zones` 中选择。
- `SubDomain`（string）：相对区域的主机记录名。示例完整名称为 `_acme-challenge.www.example.com`。
- `Offset`（integer）：首批为 0，下一批加上本批实际返回条数。
- `Limit`（integer）：当前固定每页 100 条。
- `ErrorOnEmpty`（string）：固定 `"no"`；没有记录时正常返回空列表。

没有传 `RecordType` 过滤条件，因为创建前还需要发现同名 CNAME。
参数拼写是 `SubDomain`；项目内部也再次用 `Name == name` 做精确过滤。

完整成功响应示例（一条 TXT）：

```json
{
  "Response": {
    "RequestId": "00000000-0000-4000-8000-000000000001",
    "RecordCountInfo": {
      "SubdomainCount": 1,
      "ListCount": 1,
      "TotalCount": 1
    },
    "RecordList": [
      {
        "RecordId": 123456,
        "Value": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "Status": "ENABLE",
        "UpdatedOn": "2026-09-06 12:00:00",
        "Name": "_acme-challenge.www",
        "Line": "默认",
        "LineId": "0",
        "Type": "TXT",
        "Weight": null,
        "MonitorStatus": "",
        "Remark": "certbot-dnspod-hook:0123456789abcdef0123456789abcdef",
        "TTL": 600,
        "MX": 0,
        "DefaultNS": false
      }
    ]
  }
}
```

`RecordCountInfo` 是计数对象；`RecordList` 是记录对象数组。
其中 `RecordId`、`TTL`、`MX` 为整数，`DefaultNS` 为布尔值；
本次未配置权重，`Weight` 返回 `null`；其余示例记录字段为字符串。
计数随实际查询变化，代码不依赖计数对象翻页，而是在返回页不足 100 条时停止。

`DNSPod.records()` 返回所有页中名称匹配的记录对象列表，保留每条记录的原始字段；
不返回外层 `Response`、`RequestId` 或 `RecordCountInfo`。无记录时返回 `[]`。
后续流程主要使用 `RecordId`、`Name`、`Type`、`Value`、`Remark`。

## 4. CreateRecord：创建验证 TXT

官方接口：[添加记录](https://cloud.tencent.com/document/api/1427/56180)。

当前请求体：

```json
{
  "Domain": "example.com",
  "SubDomain": "_acme-challenge.www",
  "RecordType": "TXT",
  "RecordLine": "默认",
  "Value": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "Remark": "certbot-dnspod-hook:0123456789abcdef0123456789abcdef",
  "TTL": 600
}
```

除 `TTL` 为整数外，其余参数均为字符串。
`Domain`、`SubDomain` 的含义与列表请求一致；`RecordType`、`RecordLine` 为固定值。
`Value` 来自 Certbot 的 `CERTBOT_VALIDATION`，程序要求 43 位 Base64URL 字符；
`Remark` 为固定前缀加每次操作生成的 UUID 十六进制串，用于核对归属和恢复。
`TTL` 来自配置，默认 600 秒，需符合域名套餐的限制。
程序不指定 `Status`，使用服务端默认启用状态。

完整成功响应：

```json
{
  "Response": {
    "RequestId": "00000000-0000-4000-8000-000000000002",
    "RecordId": 123456
  }
}
```

`DNSPod.create()` 校验 `Response.RecordId` 是正整数后，仅返回这个整数，
并由上层保存到本地状态。接口成功仅表示记录创建成功，随后还会单独检查 DNS 传播。

创建请求只发送一次。网络失败时，服务端可能已经完成创建；上层用
`DescribeRecordList` 按名称、类型、值及 Remark 寻找这次创建的记录。
腾讯云说明新记录存在短暂索引延迟，项目最多查询七轮、轮间等待 10 秒；
仍不确定则保留状态并失败，不再次创建。[索引延迟说明](https://cloud.tencent.com/document/api/1427/56180)

## 5. DescribeRecord：按 ID 读取一条记录

官方接口：[获取记录信息](https://cloud.tencent.com/document/api/1427/56168)。

当前请求体：

```json
{
  "Domain": "example.com",
  "RecordId": 123456
}
```

`Domain` 为字符串，`RecordId` 为整数，通常来自创建成功后保存的本地状态。

完整成功响应：

```json
{
  "Response": {
    "RequestId": "00000000-0000-4000-8000-000000000003",
    "RecordInfo": {
      "MX": 0,
      "TTL": 600,
      "Id": 123456,
      "SubDomain": "_acme-challenge.www",
      "RecordType": "TXT",
      "RecordLine": "默认",
      "RecordLineId": "0",
      "Value": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      "Weight": null,
      "Enabled": 1,
      "MonitorStatus": "",
      "Remark": "certbot-dnspod-hook:0123456789abcdef0123456789abcdef",
      "UpdatedOn": "2026-09-06 12:00:00",
      "DomainId": 87654321
    }
  }
}
```

`RecordInfo` 是一个对象。`Id`、`DomainId`、`TTL`、`MX`、`Enabled` 为整数；
本次 `Weight` 为 `null`，其余示例字段为字符串。
**详情响应中的记录编号叫 `Id`，不是请求参数中的 `RecordId`。**

`DNSPod.get()` 会统一字段，返回：

```json
{
  "RecordId": 123456,
  "Name": "_acme-challenge.www",
  "Type": "TXT",
  "Value": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "Remark": "certbot-dnspod-hook:0123456789abcdef0123456789abcdef"
}
```

如果服务端返回 `InvalidParameter.RecordIdInvalid`，该方法返回 Python `None`。
其他错误仍抛异常，不当作“记录已不存在”。

列表和详情字段不能互换：

| 含义 | 列表中的字段 | 详情中的字段 | `get()` 统一后的字段 |
| --- | --- | --- | --- |
| 记录 ID | `RecordId` | `Id` | `RecordId` |
| 主机记录 | `Name` | `SubDomain` | `Name` |
| 类型 | `Type` | `RecordType` | `Type` |
| 值 | `Value` | `Value` | `Value` |
| 备注 | `Remark` | `Remark` | `Remark` |
| 线路 | `Line` / `LineId` | `RecordLine` / `RecordLineId` | 不保留 |
| 启用状态 | `Status`，字符串 | `Enabled`，整数 | 不保留 |

本次实测发现原测试样本错误地把详情字段写成了 `RecordId`，使错误实现也能通过模拟测试。
已经改为真实的 `RecordInfo.Id` 并修正映射；该测试使用不包含 `RecordInfo.RecordId`
的响应，检查转换后的五个字段，防止这个错误回归。

## 6. DeleteRecord：删除已核对的记录

官方接口：[删除记录](https://cloud.tencent.com/document/api/1427/56176)。

当前请求体：

```json
{
  "Domain": "example.com",
  "RecordId": 123456
}
```

`Domain` 为字符串，`RecordId` 为整数。
上层 cleanup 先通过详情接口核对记录归属；这个 API 自身仅按指定 ID 删除，
不会替我们检查 Remark 或 TXT 值。

完整成功响应：

```json
{
  "Response": {
    "RequestId": "00000000-0000-4000-8000-000000000004"
  }
}
```

成功响应只有请求 ID，没有 `Success: true` 或被删除记录的完整信息。
`DNSPod.delete()` 正常结束返回 Python `None`。
`InvalidParameter.RecordIdInvalid` 也视为已经删除，以支持重复清理；其他错误抛异常。
正常 cleanup 不会在删除后再发一次查询；本次联调额外查询确认了临时记录已不存在。

## 7. 失败响应与重试

服务端业务错误的外层格式如下。这里是结构示例，`Message` 文本随实际错误而变；
客户端网络错误则可能没有服务端 JSON 或请求 ID。

```json
{
  "Response": {
    "Error": {
      "Code": "InvalidParameter.RecordIdInvalid",
      "Message": "错误说明文本"
    },
    "RequestId": "00000000-0000-4000-8000-000000000005"
  }
}
```

SDK 将业务错误变为 `TencentCloudSDKException`，项目再转为 `APIError`。
日志只包含 Action、错误码和 RequestId，不打印 SDK 的错误正文或凭证。

- `CreateRecord`：不自动重发。以 `AuthFailure`、`UnauthorizedOperation`、
  `InvalidParameter`、`OperationDenied`、`LimitExceeded`、`RequestLimitExceeded`
  开头的错误被视为明确拒绝，上层删除未完成状态并报错；其他 API 错误尝试按标记找回记录。
- 其余三个 API：仅在错误码以 `RequestLimitExceeded`、`InternalError` 开头，
  或为 `ClientNetworkError`、`ClientNetworkSocketError`、`FailedOperation.FrequencyLimit`
  时，最多尝试三次，重试间隔 1、2 秒；其他错误立即抛出。
- `get/delete` 对记录 ID 不存在的特殊处理见上文；权限不足、域名不存在不能据此当作成功。
- 响应结构异常也会导致命令失败；程序不会把缺少关键字段的响应当作成功。

## 8. 其他输入输出：Certbot 与 DNS

项目自身不提供 HTTP 服务。`setup` 负责导入凭证、调用本地 Certbot reconfigure / renew
和 systemctl，不增加 DNSPod API 操作；具体参数见 README。Certbot 调用两个本地 hooks：

| 命令 | 输入 | 成功输出 |
| --- | --- | --- |
| `auth` | 环境变量 `CERTBOT_IDENTIFIER`（兼容 `CERTBOT_DOMAIN`）及 `CERTBOT_VALIDATION` | stdout 输出 64 位十六进制状态 ID，退出码 0 |
| `cleanup` | 相同挑战变量；若有 `CERTBOT_AUTH_OUTPUT`，需与该挑战状态 ID 一致 | stdout 无业务输出，退出码 0 |
| `cleanup --state-id ID` | 指定保存的本地状态，不需要挑战环境变量 | 同上 |
| `status` | 配置中的本地状态目录 | JSON 数组，元素包含 `state_id`、`domain`、`record_id` |

状态 ID 是规范化域名、换行和挑战值的 SHA-256 十六进制摘要，
不是 DNSPod 的整数记录 ID。命令失败通常退出 1，中断退出 130；日志写入 stderr。

DNS 传播检查使用 dnspython，经本机解析器查询 SOA（查找区域）、NS、NS 的 A/AAAA，
再向各权威 NS 直接查询目标 TXT，使用 UDP 并支持 TCP 回退。
检查权威回答标记、完整名称和 TXT 值；这些是 DNS 协议请求，不是腾讯云 API，
不增加 CAM API 操作权限，也不会调用 Let's Encrypt 的签发接口。
证书申请及 CA 验证由 Certbot 负责。

## 9. 本次验证覆盖范围

2026-09-06 使用真实 DNSPod 凭证，在一个独立的随机子域名下完成：
列表查询 → 创建临时 TXT → 权威 DNS 可见 → 详情读取与字段核对 → 删除 → 确认不存在。
四个 API、项目 auth/cleanup 核心流程和权威 DNS 检查均通过，临时本地状态也已移除。

这验证了测试时凭证对目标区域的四项操作可用；尚未执行 Certbot / Let's Encrypt staging
完整签发或续期，也未接入生产续期配置。上线前仍需按 README 完成测试续期。
