"""DNSPod API 3.0 adapter, using Tencent Cloud's official signing client."""

import time

from tencentcloud.common import credential
from tencentcloud.common.common_client import CommonClient
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile

from .config import Config, HookError


class APIError(HookError):
    def __init__(self, action: str, code: str, request_id: str | None = None):
        self.code = code
        super().__init__(f"DNSPod {action}: {code} (request ID: {request_id or 'unavailable'})")


class DNSPod:
    def __init__(self, config: Config):
        if not config.secret_id or not config.secret_key:
            raise HookError("Set Tencent Cloud secret_id and secret_key in config or environment")
        profile = ClientProfile()
        profile.httpProfile = HttpProfile(endpoint="dnspod.tencentcloudapi.com", reqTimeout=20)
        self.client = CommonClient(
            "dnspod",
            "2021-03-23",
            credential.Credential(config.secret_id, config.secret_key, config.token or None),
            "",
            profile,
        )

    def call(self, action: str, **params) -> dict:
        # CreateRecord is deliberately called exactly once: a timeout is ambiguous.
        attempts = 1 if action == "CreateRecord" else 3
        for attempt in range(attempts):
            try:
                return self.client.call_json(action, params)["Response"]
            except TencentCloudSDKException as exc:
                code = exc.get_code() or "UnknownError"
                transient = code.startswith(("RequestLimitExceeded", "InternalError")) or code in {
                    "ClientNetworkError",
                    "ClientNetworkSocketError",
                    "FailedOperation.FrequencyLimit",
                }
                if not transient or attempt == attempts - 1:
                    # SDK messages may include request data. Log only code and request ID.
                    raise APIError(action, code, exc.get_request_id()) from None
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def records(self, zone: str, name: str) -> list[dict]:
        records = []
        offset = 0
        while True:
            response = self.call(
                "DescribeRecordList",
                Domain=zone,
                SubDomain=name,
                Offset=offset,
                Limit=100,
                ErrorOnEmpty="no",
            )
            page = response.get("RecordList", [])
            records.extend(r for r in page if r["Name"] == name)
            if len(page) < 100:
                return records
            offset += len(page)

    def create(self, state: dict, ttl: int) -> int:
        response = self.call(
            "CreateRecord",
            Domain=state["zone"],
            SubDomain=state["name"],
            RecordType="TXT",
            RecordLine="默认",
            Value=state["value"],
            Remark=state["marker"],
            TTL=ttl,
        )
        record_id = response["RecordId"]
        if type(record_id) is not int or record_id <= 0:
            raise HookError("DNSPod returned an invalid record ID; pending state retained")
        return record_id

    def get(self, zone: str, record_id: int) -> dict | None:
        try:
            record = self.call("DescribeRecord", Domain=zone, RecordId=record_id)["RecordInfo"]
        except APIError as exc:
            if exc.code == "InvalidParameter.RecordIdInvalid":
                return None
            raise
        return {
            "RecordId": record["Id"],
            "Name": record["SubDomain"],
            "Type": record["RecordType"],
            "Value": record["Value"],
            "Remark": record.get("Remark", ""),
        }

    def delete(self, zone: str, record_id: int) -> None:
        try:
            self.call("DeleteRecord", Domain=zone, RecordId=record_id)
        except APIError as exc:
            if exc.code != "InvalidParameter.RecordIdInvalid":
                raise
