from unittest.mock import Mock

import pytest
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException

from certbot_dnspod_hook.provider import APIError, DNSPod


def test_official_sdk_response_envelope_and_endpoint(sdk_config):
    api = DNSPod(sdk_config)
    assert api.client.profile.httpProfile.endpoint == "dnspod.tencentcloudapi.com"
    assert api.client.profile.retryer is None
    api.client.call_json = Mock(return_value={"Response": {"RecordId": 12}})
    assert (
        api.create(
            {
                "zone": "example.com",
                "name": "_acme-challenge",
                "value": "a" * 43,
                "marker": "owned",
            },
            600,
        )
        == 12
    )
    action, params = api.client.call_json.call_args.args
    assert action == "CreateRecord"
    assert params["RecordType"] == "TXT"
    assert params["Remark"] == "owned"
    assert params["RecordLine"] == "默认"


def test_pagination_uses_exact_subdomain(sdk_config):
    api = DNSPod(sdk_config)
    row = {"Name": "_acme-challenge", "RecordId": 1}
    api.client.call_json = Mock(
        side_effect=[
            {"Response": {"RecordList": [row] * 100}},
            {"Response": {"RecordList": [{"Name": "www"}]}},
        ]
    )
    assert len(api.records("example.com", "_acme-challenge")) == 100
    calls = api.client.call_json.call_args_list
    assert calls[0].args[1]["SubDomain"] == "_acme-challenge"
    assert calls[1].args[1]["Offset"] == 100
    assert calls[1].args[1]["ErrorOnEmpty"] == "no"


@pytest.mark.parametrize(
    "action,attempts", [("CreateRecord", 1), ("DescribeRecord", 3), ("DeleteRecord", 3)]
)
def test_retry_policy_and_secret_redaction(sdk_config, monkeypatch, action, attempts):
    api = DNSPod(sdk_config)
    api.client.call_json = Mock(
        side_effect=TencentCloudSDKException(
            "ClientNetworkError", "test-secret should not be logged", "request-123"
        )
    )
    monkeypatch.setattr("certbot_dnspod_hook.provider.time.sleep", lambda _: None)
    with pytest.raises(APIError) as exc:
        api.call(action)
    assert api.client.call_json.call_count == attempts
    assert "test-secret" not in str(exc.value)
    assert "request-123" in str(exc.value)


def test_permission_failure_is_not_retried(sdk_config):
    api = DNSPod(sdk_config)
    api.client.call_json = Mock(side_effect=TencentCloudSDKException("UnauthorizedOperation"))
    with pytest.raises(APIError):
        api.call("DescribeRecord")
    assert api.client.call_json.call_count == 1


def test_describe_normalizes_api_fields(sdk_config):
    api = DNSPod(sdk_config)
    api.client.call_json = Mock(
        return_value={
            "Response": {
                "RequestId": "request-describe",
                "RecordInfo": {
                    # DNSPod detail uses Id; only create/list use RecordId.
                    "Id": 7,
                    "SubDomain": "_acme-challenge",
                    "RecordType": "TXT",
                    "RecordLine": "默认",
                    "RecordLineId": "0",
                    "Value": "token",
                    "Weight": None,
                    "MX": 0,
                    "TTL": 600,
                    "Enabled": 1,
                    "MonitorStatus": "",
                    "Remark": "marker",
                    "UpdatedOn": "2026-09-06 12:00:00",
                    "DomainId": 42,
                },
            }
        }
    )
    assert api.get("example.com", 7) == {
        "RecordId": 7,
        "Name": "_acme-challenge",
        "Type": "TXT",
        "Value": "token",
        "Remark": "marker",
    }


def test_missing_record_is_idempotent_but_domain_error_is_not(sdk_config):
    api = DNSPod(sdk_config)
    api.client.call_json = Mock(
        side_effect=TencentCloudSDKException("InvalidParameter.RecordIdInvalid")
    )
    assert api.get("example.com", 7) is None
    api.delete("example.com", 7)
    api.client.call_json = Mock(
        side_effect=TencentCloudSDKException("InvalidParameterValue.DomainNotExists")
    )
    with pytest.raises(APIError):
        api.get("example.com", 7)
