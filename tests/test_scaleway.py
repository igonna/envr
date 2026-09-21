"""Scaleway API contract and billing boundaries; no billable resources."""
import base64
import contextlib
import copy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from test_envr import envr


PROJECT = "11111111-1111-4111-8111-111111111111"
SERVER = "22222222-2222-4222-8222-222222222222"
ZONE = "fr-par-1"


def offer(name="EM-A116X-SSD", **changes):
    value = {
        "id": name + "-hourly", "name": name, "enable": True, "stock": "available",
        "subscription_period": "hourly", "fee": {"currency_code": "EUR", "units": 0, "nanos": 0},
        "price_per_hour": {"currency_code": "EUR", "units": 0, "nanos": 77000000},
        "price_per_month": {"currency_code": "EUR", "units": 27, "nanos": 990000000},
        "memories": [{"capacity": envr.SCALEWAY_PLANS[name][0] * 1024 ** 3}],
        "cpus": [{"name": "Intel Xeon", "core_count": 4, "thread_count": 8}],
        "disks": [{"capacity": 1020000000000, "type": "SSD"}] * 2,
        "incompatible_os_ids": [],
    }
    value.update(changes)
    return value


class ScalewayTests(unittest.TestCase):
    def setUp(self):
        self.api = envr.ScalewayAPI(PROJECT + "\nsecret")
        self.api.regions = (ZONE,)
        self.offers = [offer()]
        self.os = {"id": "ubuntu-id", "name": "Ubuntu", "version": "24.04 LTS",
                   "enabled": True, "allowed": True, "cloud_init_supported": True,
                   "license_required": False}
        self.server = {"id": SERVER, "project_id": PROJECT, "name": envr.LABEL,
                       "zone": ZONE, "offer_id": self.offers[0]["id"],
                       "status": "ready", "install": {"status": "completed"},
                       "tags": [envr.MANAGEMENT_TAG],
                       "ips": [{"version": "IPv6", "address": "::1"},
                               {"version": "IPv4", "address": "192.0.2.1"}]}
        self.calls = []

        def request(method, resource, **kwargs):
            self.calls.append((method, resource, kwargs))
            if resource.endswith("/os"):
                return {"os": [self.os], "total_count": 1}
            if resource.endswith("/offers"):
                return {"offers": self.offers, "total_count": len(self.offers)}
            if "/offers/" in resource:
                return next(o for o in self.offers if resource.endswith("/" + o["id"]))
            if resource.endswith("/servers"):
                if method == "POST":
                    return copy.deepcopy(self.server)
                return {"servers": [self.server], "total_count": 1}
            if "/servers/" in resource:
                return copy.deepcopy(self.server) if method == "GET" else {}
            if resource == "iam/v1alpha1/ssh-keys":
                if method == "POST":
                    return {"id": "key", **kwargs["payload"]}
                return {"ssh_keys": [{"id": "key", "name": "envr-device-test",
                                      "public_key": "ssh-ed25519 TEST", "disabled": False},
                                     {"id": "disabled", "disabled": True}], "total_count": 2}
            if resource == "account/v3/projects/" + PROJECT:
                return {"id": PROJECT}
            raise AssertionError((method, resource, kwargs))

        self.api.raw.request = mock.Mock(side_effect=request)

    def payload(self):
        return {"region": ZONE, "plan": "EM-A116X-SSD", "os_id": "ubuntu-24.04",
                "label": envr.LABEL, "hostname": "envr", "tags": [envr.MANAGEMENT_TAG],
                "sshkey_id": ["key"], "user_data": base64.b64encode(b"#cloud-config\n").decode()}

    def test_catalog_excludes_commitments_fees_unknown_billing_and_wrong_sizes(self):
        self.offers += [offer(subscription_period="monthly"), offer(subscription_period="unknown_subscription_period"),
                        offer(fee={"units": 19}), offer(stock="empty"), offer(enable=False),
                        offer(memories=[{"capacity": 64 * 1024 ** 3}]),
                        offer(incompatible_os_ids=["ubuntu-id"])]
        plans = self.api.get("plans")["plans"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["hourly_cost"], .077)
        self.assertIsNone(plans[0]["monthly_cost"])
        self.assertNotIn("/month", envr.compact_price(plans[0]))
        self.assertIn("no setup fee", envr.compact_price(plans[0]))

    def test_missing_fee_fails_closed(self):
        self.offers[0].pop("fee")
        with self.assertRaisesRegex(envr.EnvrError, "verifiable"):
            self.api.get("plans")

    def test_os_without_cloud_init_not_offered(self):
        self.os["cloud_init_supported"] = False
        self.assertEqual(self.api.get("plans"), {"plans": []})
        self.assertEqual(self.api.get("os"), {"os": []})

    def test_create_uses_hourly_uuid_project_keys_and_base64_cloud_init(self):
        self.api.get("plans")
        instance = self.api.post("instances", self.payload())["instance"]
        payload = next(kw["payload"] for method, path, kw in self.calls if method == "POST")
        self.assertEqual(payload["offer_id"], "EM-A116X-SSD-hourly")
        self.assertEqual(payload["project_id"], PROJECT)
        self.assertEqual(payload["install"]["os_id"], "ubuntu-id")
        self.assertEqual(payload["install"]["ssh_key_ids"], ["key"])
        self.assertEqual(payload["option_ids"], [])
        self.assertEqual(base64.b64decode(payload["user_data"]), b"#cloud-config\n")
        self.assertEqual(instance["id"], ZONE + ":" + SERVER)
        self.assertEqual(instance["main_ip"], "192.0.2.1")
        self.assertEqual(instance["status"], "active")

    def test_changed_price_or_subscription_blocks_purchase(self):
        for change in ({"price_per_hour": {"units": 1, "currency_code": "EUR"}},
                       {"subscription_period": "monthly"}, {"fee": {"units": 1}},
                       {"stock": "empty"}):
            with self.subTest(change=change):
                self.offers = [offer()]
                self.api.get("plans")
                self.offers = [offer(**change)]
                with self.assertRaisesRegex(envr.EnvrError, "offer changed"):
                    self.api.post("instances", self.payload())
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))

    def test_installation_state_is_not_ready_before_os_install_finishes(self):
        self.server["install"]["status"] = "installing"
        instance = self.api.get("instances")["instances"][0]
        self.assertNotEqual(instance["status"], "active")
        self.server["install"]["status"] = "error"
        self.assertEqual(self.api.get("instances")["instances"][0]["status"], "error")

    def test_inventory_is_project_scoped_and_rename_delete_survive_adapter_restart(self):
        instance = self.api.get("instances")["instances"][0]
        inventory_call = next(kw for method, path, kw in self.calls if path.endswith("/servers"))
        self.assertEqual(inventory_call["query"]["project_id"], PROJECT)
        self.api._offers.clear()
        self.api.patch("instances/" + instance["id"], {"label": envr.RESIZE_SOURCE_LABEL})
        self.api.delete("instances/" + instance["id"])
        mutations = [(m, p, kw) for m, p, kw in self.calls if m in {"PATCH", "DELETE"}]
        self.assertEqual(mutations[0][2]["payload"], {"name": envr.RESIZE_SOURCE_LABEL})
        self.assertEqual(mutations[1][1], f"baremetal/v1/zones/{ZONE}/servers/{SERVER}")

    def test_foreign_project_cannot_be_deleted(self):
        self.server["project_id"] = "different-project"
        with self.assertRaisesRegex(envr.EnvrError, "different project"):
            self.api.delete("instances/" + ZONE + ":" + SERVER)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.calls))

    def test_ssh_keys_project_scope_and_disabled_filter(self):
        self.assertEqual(len(self.api.get("ssh-keys")["ssh_keys"]), 1)
        key = self.api.post("ssh-keys", {"name": "envr-device-new", "ssh_key": "ssh-ed25519 NEW"})
        self.assertEqual(key["ssh_key"]["ssh_key"], "ssh-ed25519 NEW")
        self.assertEqual(self.calls[-1][2]["payload"]["project_id"], PROJECT)

    def test_pagination_and_auth_header(self):
        self.api.raw.request = mock.Mock(side_effect=[
            {"ssh_keys": [{"id": "a"}], "total_count": 2},
            {"ssh_keys": [{"id": "b"}], "total_count": 2}])
        self.assertEqual(len(self.api._all("keys", "ssh_keys")), 2)
        self.assertEqual(self.api.raw.request.call_args.kwargs["query"]["page"], 2)
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{}'
        envr.ScalewayAPI(PROJECT + "\nsecret", opener=opener).get("account")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-auth-token"), "secret")
        self.assertIsNone(request.get_header("Authorization"))

    def test_all_three_menu_options_require_kvm(self):
        self.offers = [offer(name) for name in envr.SCALEWAY_PLANS]
        with tempfile.TemporaryDirectory() as directory:
            app = envr.Envr(Path(directory))
            options = app.resize_options(self.api)
            self.assertEqual(len(options), 3)
            self.assertEqual({o["ram_gb"] for o in options}, {16, 32})
            self.assertTrue(all("bare metal" in o["kvm"] for o in options))
            app.remote_script = mock.Mock(return_value=SimpleNamespace(returncode=0))
            app.remote_command = mock.Mock(return_value=SimpleNamespace(returncode=1))
            app.sync_from_server = mock.Mock()
            for option in options:
                with self.assertRaisesRegex(envr.EnvrError, "KVM is not usable"):
                    app.validate_resize_target({"provider": "scaleway", "plan": option["plan_id"]})
            app.sync_from_server.assert_not_called()

    def test_differently_priced_region_is_not_a_silent_fallback(self):
        self.api.regions = (ZONE, "nl-ams-1")
        original = self.api.raw.request.side_effect

        def request(method, path, **kwargs):
            if "nl-ams-1/offers" in path:
                return {"offers": [offer(price_per_hour={"units": 1, "currency_code": "EUR"})], "total_count": 1}
            return original(method, path, **kwargs)

        self.api.raw.request.side_effect = request
        plan = self.api.get("plans")["plans"][0]
        self.assertEqual(plan["locations"], [ZONE])
        self.assertEqual(self.api.get("regions/nl-ams-1/availability"), {"available_plans": []})

    def test_ambiguous_creation_does_not_purchase_in_another_zone(self):
        with tempfile.TemporaryDirectory() as directory:
            app = envr.Envr(Path(directory))
            app.plan_catalog = mock.Mock(return_value={"EM-A116X-SSD": {"ram": 32768}})
            app.ubuntu_os_id = mock.Mock(return_value="ubuntu-24.04")
            app.available_regions = mock.Mock(return_value=[ZONE, "nl-ams-1"])
            app.ensure_vps_key = mock.Mock(return_value="ssh-ed25519 TEST")
            app.ensure_vultr_ssh_key = mock.Mock(return_value="key")
            app.managed_vultr_ssh_keys = mock.Mock(return_value=[])
            app.reconcile_creation = mock.Mock(return_value=[])
            self.api.post = mock.Mock(side_effect=envr.ApiError(503, "timeout", provider="Scaleway"))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(envr.EnvrError, "outcome is unknown"):
                app.create_instance(self.api, 32)
            self.api.post.assert_called_once()
            app.reconcile_creation.assert_called_once_with(self.api, label=envr.LABEL, wait_seconds=20)


if __name__ == "__main__":
    unittest.main()
