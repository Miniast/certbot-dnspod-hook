import pytest

from certbot_dnspod_hook.config import HookError
from certbot_dnspod_hook.core import challenge
from certbot_dnspod_hook.provider import APIError

VALUE, OTHER = "a" * 43, "b" * 43


def test_wildcard_apex_and_unrelated_txt_cleanup_independently(setup_hook):
    hook, api, store = setup_hook
    foreign = {"RecordId": 99, "Name": "_acme-challenge", "Type": "TXT", "Value": "external"}
    api.data[99] = foreign.copy()
    first = hook.auth("example.com", VALUE)
    second = hook.auth("*.example.com", OTHER)
    assert first != second
    assert {r["Name"] for r in api.data.values()} == {"_acme-challenge"}
    hook.cleanup(first)
    assert {r["Value"] for r in api.data.values()} == {OTHER, "external"}
    hook.cleanup(second)
    hook.cleanup(second)
    assert api.data == {99: foreign}
    assert not list(store.directory.glob("*.json"))


def test_repeat_auth_reuses_owned_record(setup_hook):
    hook, api, _ = setup_hook
    key = hook.auth("WWW.Example.com.", VALUE)
    assert hook.auth("www.example.com", VALUE) == key
    assert api.creates == 1


@pytest.mark.parametrize("kind,value", [("CNAME", "target.example.net"), ("TXT", VALUE)])
def test_does_not_adopt_existing_records(setup_hook, kind, value):
    hook, api, store = setup_hook
    api.data[99] = {"Name": "_acme-challenge", "Type": kind, "Value": value}
    with pytest.raises(HookError):
        hook.auth("example.com", VALUE)
    assert api.creates == 0
    assert not list(store.directory.glob("*.json"))


@pytest.mark.parametrize(
    "field,new", [("Type", "A"), ("Name", "www"), ("Value", OTHER), ("Remark", "someone-else")]
)
def test_refuses_to_delete_modified_record(setup_hook, field, new):
    hook, api, store = setup_hook
    key = hook.auth("example.com", VALUE)
    api.data[1][field] = new
    with pytest.raises(HookError, match="refusing deletion"):
        hook.cleanup(key)
    assert api.deleted == []
    assert store.read(key) is not None


def test_cleanup_of_already_removed_record(setup_hook):
    hook, api, store = setup_hook
    key = hook.auth("example.com", VALUE)
    api.data.clear()
    hook.cleanup(key)
    assert store.read(key) is None


def test_cleanup_failure_retains_state(setup_hook, monkeypatch):
    hook, api, store = setup_hook
    key = hook.auth("example.com", VALUE)

    def fail(*args):
        raise APIError("DeleteRecord", "ClientNetworkError")

    monkeypatch.setattr(api, "delete", fail)
    with pytest.raises(HookError):
        hook.cleanup(key)
    assert store.read(key)["record_id"] == 1


def test_auth_timeout_cleans_up(setup_hook):
    hook, api, store = setup_hook

    def fail(*args):
        raise HookError("DNS propagation timed out")

    hook.wait = fail
    with pytest.raises(HookError, match="timed out"):
        hook.auth("example.com", VALUE)
    assert api.deleted == [1]
    assert not list(store.directory.glob("*.json"))


def test_lost_create_response_recovers_through_index_delay(setup_hook, monkeypatch):
    hook, api, store = setup_hook
    original_create, original_records = api.create, api.records

    def lost_response(state, ttl):
        original_create(state, ttl)
        raise APIError("CreateRecord", "ClientNetworkError")

    calls = 0

    def delayed_records(zone, name):
        nonlocal calls
        calls += 1
        return [] if calls <= 4 else original_records(zone, name)

    monkeypatch.setattr(api, "create", lost_response)
    monkeypatch.setattr(api, "records", delayed_records)
    monkeypatch.setattr("certbot_dnspod_hook.core.time.sleep", lambda _: None)
    key = hook.auth("example.com", VALUE)
    assert api.creates == 1
    assert calls == 5
    assert store.read(key)["record_id"] == 1
    hook.cleanup(key)


def test_unknown_create_is_not_repeated(setup_hook, monkeypatch):
    hook, api, store = setup_hook

    def fail(*args):
        api.creates += 1
        raise APIError("CreateRecord", "ClientNetworkError")

    monkeypatch.setattr(api, "create", fail)
    monkeypatch.setattr("certbot_dnspod_hook.core.time.sleep", lambda _: None)
    for _ in range(2):
        with pytest.raises(HookError, match="outcome is still unknown"):
            hook.auth("example.com", VALUE)
    assert api.creates == 1
    assert len(list(store.directory.glob("*.json"))) == 1


def test_explicit_rejection_discards_pending_state(setup_hook, monkeypatch):
    hook, api, store = setup_hook

    def fail(*args):
        raise APIError("CreateRecord", "UnauthorizedOperation")

    monkeypatch.setattr(api, "create", fail)
    with pytest.raises(HookError):
        hook.auth("example.com", VALUE)
    assert not list(store.directory.glob("*.json"))


def test_recovers_after_process_dies_before_record_id_is_saved(setup_hook, monkeypatch):
    hook, api, store = setup_hook
    key = hook.auth("example.com", VALUE)
    state = store.read(key)
    state["record_id"] = None
    store.write(key, state)
    monkeypatch.setattr("certbot_dnspod_hook.core.time.sleep", lambda _: None)
    hook.cleanup(key)
    assert api.deleted == [1]


def test_invalid_state_never_deletes(setup_hook):
    hook, api, store = setup_hook
    key = hook.auth("example.com", VALUE)
    state = store.read(key)
    state["name"] = "www"
    store.write(key, state)
    with pytest.raises(HookError, match="Invalid or incompatible"):
        hook.cleanup(key)
    assert not api.deleted


@pytest.mark.parametrize("domain", ["evilexample.com", "example.com.evil.org"])
def test_zone_matching_respects_label_boundaries(setup_hook, domain):
    hook, api, _ = setup_hook
    with pytest.raises(HookError, match="outside"):
        hook.auth(domain, VALUE)
    assert not api.creates


@pytest.mark.parametrize(
    "domain,value",
    [("1.2.3.4", VALUE), ("a..example.com", VALUE), ("example.com", "unsafe\nvalue")],
)
def test_invalid_challenge(domain, value):
    with pytest.raises((HookError, ValueError)):
        challenge(domain, value)
