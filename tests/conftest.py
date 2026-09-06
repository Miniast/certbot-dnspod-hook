from dataclasses import replace

import pytest

from certbot_dnspod_hook.config import Config
from certbot_dnspod_hook.core import Hook
from certbot_dnspod_hook.state import StateStore


class FakeDNSPod:
    def __init__(self):
        self.data, self.creates, self.deleted = {}, 0, []

    def records(self, zone, name):
        return [r.copy() for r in self.data.values() if r["Name"] == name]

    def create(self, state, ttl):
        self.creates += 1
        self.data[self.creates] = {
            "RecordId": self.creates,
            "Name": state["name"],
            "Type": "TXT",
            "Value": state["value"],
            "Remark": state["marker"],
        }
        return self.creates

    def get(self, zone, record_id):
        return self.data.get(record_id)

    def delete(self, zone, record_id):
        self.deleted.append(record_id)
        self.data.pop(record_id, None)


@pytest.fixture
def config(tmp_path):
    return Config(
        zones=("example.com",),
        state_dir=tmp_path / "state",
        propagation_seconds=0,
        propagation_timeout=30,
    )


@pytest.fixture
def setup_hook(config):
    api, store = FakeDNSPod(), StateStore(config.state_dir)
    return Hook(config, api, store, lambda *args: None), api, store


@pytest.fixture
def sdk_config(config):
    return replace(config, secret_id="test-id", secret_key="test-secret")
