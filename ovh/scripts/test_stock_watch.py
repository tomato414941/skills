import json
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import stock_watch


def rate(capacity, price):
    return {"mode": "default", "capacities": [capacity], "commitment": 0,
            "interval": 1 if capacity == "renew" else 0,
            "intervalUnit": "month" if capacity == "renew" else "none",
            "price": int(price * 100000000)}


def target():
    return {"name": "sample-server", "kind": "dedicated", "plan": "dedicated-base",
            "memory": "ram-32", "storage": "ssd-2x512", "places": ["any"], "os": "none_64.en",
            "options": [], "description": "sample-server", "currency": "USD",
            "max_monthly_price": "12", "max_first_price": "25"}


class FakeOvh:
    def __init__(self):
        self.calls = []
        self.monthly = 10
        self.installation = 10
        self.tax_rate = 0
        self.stock = "1H-low"
        self.currency = "USD"
        self.lose_stock = False
        self.timeout_checkout = False
        self.quote_hook = None
        self.extra_requirement = False
        self.unknown_checkout = False
        self.requests = []

    def __call__(self, method, path, body=None, signed=True):
        self.calls.append((method, path, body))
        if path.startswith("/dedicated/server/datacenter/availabilities"):
            return 200, [
                {"memory": "ram-128", "storage": "ssd-2x512",
                 "datacenters": [{"datacenter": "sgp", "availability": "1H-high"}]},
                {"memory": "ram-32", "storage": "hdd-2x4000",
                 "datacenters": [{"datacenter": "sgp", "availability": "1H-high"}]},
                {"memory": "ram-32", "storage": "ssd-2x512",
                 "datacenters": [{"datacenter": "gra", "availability": self.stock}]},
            ]
        if path.startswith("/order/catalog/public/eco"):
            plan = {"planCode": "dedicated-base", "pricings": [rate("renew", self.monthly), rate("installation", self.installation)],
                    "addonFamilies": [
                        {"name": "memory", "mandatory": True, "addons": ["ram-128-option", "ram-32-option"], "default": "ram-128-option"},
                        {"name": "storage", "mandatory": True, "addons": ["ssd-2x512-option"], "default": "ssd-2x512-option"},
                        {"name": "bandwidth", "mandatory": True, "addons": ["bandwidth-option"], "default": "bandwidth-option"},
                    ],
                    "configurations": [
                        {"name": "dedicated_datacenter", "values": ["gra"]},
                        {"name": "dedicated_os", "values": ["none_64.en"]},
                        {"name": "region", "values": ["europe", "canada"]},
                    ]}
            return 200, {"locale": {"currencyCode": "USD"}, "plans": [plan],
                         "addons": [{"planCode": code, "pricings": [rate("renew", 0)]}
                                    for code in ("ram-32-option", "ssd-2x512-option", "bandwidth-option")]}
        if path == "/order/cart":
            return 200, {"cartId": "sample-cart"}
        if path.endswith("/eco"):
            return 200, {"itemId": 7}
        if path.endswith("/requiredConfiguration"):
            return 200, [{"label": "unconfigured-feature", "required": True}] if self.extra_requirement else []
        if path.endswith("/checkout"):
            if method == "GET":
                if self.lose_stock:
                    return 400, "dedicated-base.ram-32.ssd-2x512 is not available in gra"
                if self.quote_hook:
                    self.quote_hook()
                before = self.monthly + self.installation
                return 200, {"prices": {"withoutTax": {"value": before, "currencyCode": self.currency},
                                         "withTax": {"value": round(before * (1 + self.tax_rate), 2), "currencyCode": self.currency}}}
            if self.timeout_checkout:
                raise TimeoutError("connection closed after submission")
            return 200, {} if self.unknown_checkout else {"orderId": 42}
        return 200, None

    def checkouts(self):
        return [call for call in self.calls if call[0] == "POST" and call[1].endswith("/checkout")]


class FakeVps(FakeOvh):
    def __init__(self):
        super().__init__()
        self.installation = 0
        self.linux_available = True

    def __call__(self, method, path, body=None, signed=True):
        if path.startswith("/vps/order/rule/datacenter"):
            self.calls.append((method, path, body))
            return 200, {"datacenters": [
                {"datacenter": "GRA", "status": "available", "linuxStatus": "out-of-stock"},
                {"datacenter": "SGP", "status": "available", "linuxStatus": "available" if self.linux_available else "out-of-stock"},
            ]}
        if path.startswith("/order/catalog/public/vps"):
            self.calls.append((method, path, body))
            return 200, {"locale": {"currencyCode": "USD"},
                         "plans": [{"planCode": "vps-2027-model2", "pricings": [rate("renew", self.monthly)]}],
                         "addons": [{"planCode": code, "pricings": [rate("renew", 0)]}
                                    for code in ("option-storage-local-2027-model2", "option-auto-backup-2027-1-model2")]}
        if method == "POST" and path.endswith("/vps"):
            self.calls.append((method, path, body))
            return 200, {"itemId": 7}
        return super().__call__(method, path, body, signed=signed)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.timer = []
        self.data = {"subsidiary": "ASIA", "api_base": "https://example.invalid/1.0", "targets": [target()]}
        self.write()

    def write(self):
        stock_watch.atomic_json(self.root / "targets.json", self.data)

    def watch(self, api):
        return stock_watch.Watch(self.root, http=api, timer=self.timer.append)

    def result(self, status):
        return json.loads((self.root / f"{status}-sample-server.json").read_text())

    def test_orders_only_the_approved_memory_storage_and_one_server(self):
        api = FakeOvh()
        self.watch(api).run()
        self.assertEqual(self.result("ordered")["orderId"], 42)
        product = next(body for method, path, body in api.calls if method == "POST" and path.endswith("/eco"))
        self.assertEqual(product["quantity"], 1)
        selected = [body["planCode"] for method, path, body in api.calls if method == "POST" and path.endswith("/eco/options")]
        self.assertEqual(selected, ["ram-32-option", "ssd-2x512-option", "bandwidth-option"])

    def test_waits_for_matching_hardware_and_allowed_locations(self):
        api = FakeOvh()
        self.data["targets"][0]["places"] = ["SGP"]
        self.write()
        self.watch(api).run()
        self.assertFalse(api.checkouts())
        self.assertEqual(self.watch(api).status(target()), "waiting")

    def test_waits_when_matching_stock_is_unavailable_or_unknown(self):
        for availability in ("unavailable", "unknown", "soon"):
            with self.subTest(availability=availability):
                api = FakeOvh()
                api.stock = availability
                self.watch(api).run()
                self.assertFalse(api.checkouts())
                self.assertEqual(self.watch(api).status(target()), "waiting")

    def test_checks_stock_without_creating_a_cart(self):
        api = FakeOvh()
        with contextlib.redirect_stdout(io.StringIO()):
            self.watch(api).check()
        self.assertTrue(api.calls)
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_stops_when_recurring_prices_exceed_the_monthly_cap(self):
        api = FakeOvh()
        api.monthly = 13
        self.watch(api).run()
        self.assertEqual(self.result("stopped")["reason"], "price above cap")
        self.assertFalse(api.checkouts())

    def test_accounts_for_tax_when_checking_the_monthly_cap(self):
        api = FakeOvh()
        api.tax_rate = 0.25
        self.data["targets"][0]["max_first_price"] = "30"
        self.write()
        self.watch(api).run()
        self.assertEqual(self.result("stopped")["reason"], "price above cap")
        self.assertFalse(api.checkouts())

    def test_checks_installation_fees_against_a_separate_first_payment_cap(self):
        api = FakeOvh()
        api.installation = 20
        self.watch(api).run()
        self.assertEqual(self.result("stopped")["reason"], "price above cap")
        self.assertFalse(api.checkouts())

    def test_checks_explicit_prices_before_tax(self):
        api = FakeOvh()
        self.data["targets"][0]["max_monthly_without_tax"] = "9"
        self.write()
        self.watch(api).run()
        self.assertEqual(self.result("stopped")["reason"], "price above cap")
        self.assertFalse(api.checkouts())

    def test_records_the_tax_inclusive_monthly_and_first_prices(self):
        api = FakeOvh()
        api.tax_rate = 0.09
        self.watch(api).run()
        result = self.result("ordered")
        self.assertEqual(result["monthly_price"], "10.90")
        self.assertEqual(result["first_price"], "21.8")

    def test_requires_the_approved_currency(self):
        api = FakeOvh()
        api.currency = "EUR"
        self.watch(api).run()
        self.assertEqual(self.watch(api).status(target()), "stopped")
        self.assertFalse(api.checkouts())

    def test_continues_waiting_if_stock_is_lost_during_preparation(self):
        api = FakeOvh()
        api.lose_stock = True
        self.watch(api).run()
        self.assertEqual(self.watch(api).status(target()), "waiting")
        self.assertFalse(api.checkouts())

    def test_orders_each_target_only_once_after_repeated_stock_checks(self):
        api = FakeOvh()
        self.watch(api).run()
        self.watch(api).run()
        self.assertEqual(len(api.checkouts()), 1)

    def test_keeps_checkout_timeouts_for_reconciliation_instead_of_ordering_again(self):
        api = FakeOvh()
        api.timeout_checkout = True
        self.watch(api).run()
        self.watch(api).run()
        self.assertTrue(self.result("stopped")["outcome_uncertain"])
        self.assertEqual(len(api.checkouts()), 1)

    def test_keeps_an_unrecognized_checkout_response_for_reconciliation(self):
        api = FakeOvh()
        api.unknown_checkout = True
        self.watch(api).run()
        self.watch(api).run()
        self.assertTrue(self.result("stopped")["outcome_uncertain"])
        self.assertEqual(len(api.checkouts()), 1)

    def test_respects_an_existing_pending_order_after_a_process_restart(self):
        api = FakeOvh()
        stock_watch.atomic_json(self.root / "pending-sample-server.json", {"cart_id": "sample-cart"})
        self.watch(api).run()
        self.assertEqual(self.watch(api).status(target()), "pending")
        self.assertFalse(api.checkouts())
        self.assertEqual(self.timer, [False])

    def test_observes_authorization_withdrawn_while_preparing_the_order(self):
        api = FakeOvh()
        def withdraw():
            self.data["targets"] = []
            self.write()
        api.quote_hook = withdraw
        self.watch(api).run()
        self.assertFalse(api.checkouts())
        self.assertIn("authorization changed", self.result("stopped")["reason"])

    def test_waits_when_another_process_is_already_ordering(self):
        api = FakeOvh()
        import fcntl
        with (self.root / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.watch(api).run()
        self.assertFalse(api.calls)

    def test_requires_all_mandatory_configuration_before_paying(self):
        api = FakeOvh()
        api.extra_requirement = True
        self.watch(api).run()
        self.assertEqual(self.watch(api).status(target()), "stopped")
        self.assertFalse(api.checkouts())

    def add_vps(self, api):
        self.data["targets"] = []
        self.write()
        self.watch(api).add("sample-vps", "vps-2027-model2", 12, ["any"], "Ubuntu 24.04", None, None)

    def test_orders_a_vps_with_linux_stock_through_the_common_engine(self):
        api = FakeVps()
        self.add_vps(api)
        self.watch(api).run()
        self.assertEqual(len(api.checkouts()), 1)
        places = [body["value"] for method, path, body in api.calls
                  if method == "POST" and path.endswith("/configuration") and body["label"] == "vps_datacenter"]
        self.assertEqual(places, ["SGP"])
        self.assertTrue((self.root / "ordered-sample-vps.json").exists())

    def test_waits_when_a_vps_has_only_windows_stock(self):
        api = FakeVps()
        api.linux_available = False
        self.add_vps(api)
        self.watch(api).run()
        self.assertFalse(api.checkouts())

    def test_stops_a_vps_above_its_monthly_cap(self):
        api = FakeVps()
        api.monthly = 13
        self.add_vps(api)
        self.watch(api).run()
        self.assertFalse(api.checkouts())
        result = json.loads((self.root / "stopped-sample-vps.json").read_text())
        self.assertEqual(result["reason"], "price above cap")

    def test_finishes_a_vps_once_and_stops_its_timer(self):
        api = FakeVps()
        self.add_vps(api)
        self.watch(api).run()
        self.assertEqual(self.timer, [True, False])
        self.watch(api).run()
        self.assertEqual(len(api.checkouts()), 1)

    def test_loads_an_explicit_profile_with_an_isolated_environment(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(stock_watch.__file__).resolve()),
                                 "--home", str(self.root), "list"],
                                env={"PATH": os.defpath}, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["name"], "sample-server")

    def test_stops_the_timer_selected_for_the_profile(self):
        api = FakeOvh()
        original = stock_watch.Watch
        def isolated_watch(root, timer):
            return original(root, http=api, timer=timer)
        with patch.object(stock_watch, "Watch", side_effect=isolated_watch), patch.object(stock_watch, "set_timer") as control:
            stock_watch.main(["--home", str(self.root), "--timer", "sample-watch.timer", "run"])
        control.assert_called_once_with(False, "sample-watch.timer")


if __name__ == "__main__":
    unittest.main()
