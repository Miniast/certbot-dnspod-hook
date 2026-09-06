from types import SimpleNamespace
from unittest.mock import Mock

import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.resolver
import dns.rrset
import pytest

from certbot_dnspod_hook.config import HookError
from certbot_dnspod_hook.propagation import has_txt, wait_for_txt

FQDN, VALUE = "_acme-challenge.example.com.", "a" * 43


def response(value=VALUE, authoritative=True):
    result = dns.message.make_response(dns.message.make_query(FQDN, "TXT"))
    if authoritative:
        result.flags |= dns.flags.AA
    if value is not None:
        result.answer.append(dns.rrset.from_text(FQDN, 600, "IN", "TXT", '"' + value + '"'))
    return result


def test_txt_exact_value_owner_and_authority():
    assert has_txt(response(), FQDN, VALUE)
    assert not has_txt(response("wrong"), FQDN, VALUE)
    assert not has_txt(response(authoritative=False), FQDN, VALUE)
    assert not has_txt(response(), "_acme-challenge.other.com", VALUE)
    result = response()
    result.set_rcode(dns.rcode.NXDOMAIN)
    assert not has_txt(result, FQDN, VALUE)


def test_split_strings_and_multiple_txt_values():
    result = response("unrelated")
    result.answer.append(dns.rrset.from_text(FQDN, 600, "IN", "TXT", '"aaa" "' + "a" * 40 + '"'))
    assert has_txt(result, FQDN, VALUE)


def test_cname_refused():
    result = response(None)
    result.answer.append(dns.rrset.from_text(FQDN, 600, "IN", "CNAME", "target.example.net."))
    with pytest.raises(HookError, match="CNAME"):
        has_txt(result, FQDN, VALUE)


def dns_mocks(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("certbot_dnspod_hook.propagation.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "certbot_dnspod_hook.propagation.time.sleep", lambda n: clock.__setitem__(0, clock[0] + n)
    )
    resolver = Mock()

    def resolve(name, kind, **kwargs):
        if kind == "NS":
            return [
                SimpleNamespace(target="ns1.example.net."),
                SimpleNamespace(target="ns2.example.net."),
            ]
        if kind == "AAAA":
            raise dns.resolver.NoAnswer
        return [
            SimpleNamespace(address="192.0.2.1" if str(name).startswith("ns1") else "192.0.2.2")
        ]

    resolver.resolve.side_effect = resolve
    monkeypatch.setattr("dns.resolver.Resolver", lambda: resolver)
    monkeypatch.setattr(
        "dns.resolver.zone_for_name", lambda *a, **k: dns.name.from_text("example.com")
    )
    return clock


def test_waits_for_every_nameserver_and_retries(config, monkeypatch):
    clock = dns_mocks(monkeypatch)
    calls = []

    def query(message, address, **kwargs):
        calls.append(address)
        missing = address == "192.0.2.2" and clock[0] < 5
        return response(None if missing else VALUE), False

    monkeypatch.setattr("dns.query.udp_with_fallback", query)
    wait_for_txt(config, FQDN, "example.com", VALUE)
    assert clock[0] == 5
    assert calls.count("192.0.2.1") >= 2
    assert calls.count("192.0.2.2") >= 2


def test_propagation_timeout_is_bounded(config, monkeypatch):
    clock = dns_mocks(monkeypatch)
    monkeypatch.setattr("dns.query.udp_with_fallback", lambda *a, **k: (response(None), False))
    with pytest.raises(HookError, match="timed out"):
        wait_for_txt(config, FQDN, "example.com", VALUE)
    assert clock[0] == config.propagation_timeout


def test_delegation_is_reported(config, monkeypatch):
    dns_mocks(monkeypatch)
    monkeypatch.setattr(
        "dns.resolver.zone_for_name", lambda *a, **k: dns.name.from_text("other.net")
    )
    with pytest.raises(HookError, match="delegated"):
        wait_for_txt(config, FQDN, "example.com", VALUE)
