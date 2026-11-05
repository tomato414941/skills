import json, tempfile, unittest
from pathlib import Path

import vps_watch

STOCK = {"datacenters": [
    {"datacenter": "GRA", "status": "available", "linuxStatus": "out-of-stock", "windowsStatus": "available"},
    {"datacenter": "SGP", "status": "available", "linuxStatus": "available", "windowsStatus": "available"},
    {"datacenter": "UK", "status": "out-of-stock", "linuxStatus": "out-of-stock", "windowsStatus": "out-of-stock"},
]}


class FakeOvh:
    def __init__(self, price=10.0, gone_at_checkout=False):
        self.price, self.gone_at_checkout, self.calls = price, gone_at_checkout, []

    def __call__(self, method, path, body=None, signed=True):
        self.calls.append((method, path.split("?")[0], body))
        if path.startswith("/vps/order/rule/datacenter"):
            return 200, STOCK
        if path == "/order/cart":
            return 200, {"cartId": "c"}
        if method == "POST" and path.endswith("/vps"):
            return 200, {"itemId": 7}
        if path.endswith("/checkout"):
            if method == "GET":
                return 200, {"prices": {"withTax": {"value": self.price}}}
            return (400, "This product is out of stock") if self.gone_at_checkout else (200, {"orderId": 42})
        return 200, None

    def places(self):
        return [b["value"] for _, p, b in self.calls if p.endswith("/configuration") and b["label"] == "vps_datacenter"]

    def paid(self):
        return any(m == "POST" and p.endswith("/checkout") for m, p, _ in self.calls)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "targets.json").write_text(json.dumps(
            {"subsidiary": "ASIA", "api_base": "https://example.invalid/1.0", "targets": []}))
        self.timer = []

    def watch(self, ovh):
        return vps_watch.Watch(self.root, http=ovh, timer=self.timer.append)

    def add(self, ovh, places="any"):
        self.watch(ovh).add("a", "vps-2027-model2", 12.0, vps_watch.parse_places(places), "Ubuntu 24.04", None, None)

    def test_orders_in_the_first_allowed_place_with_linux_stock(self):
        ovh = FakeOvh()
        self.add(ovh)
        self.watch(ovh).run()
        self.assertEqual(ovh.places(), ["SGP"])
        self.assertTrue((self.root / "ordered-a.json").exists())

    def test_waits_while_the_allowed_places_have_only_windows_stock(self):
        ovh = FakeOvh()
        self.add(ovh, places="GRA,UK")
        self.watch(ovh).run()
        self.assertEqual(ovh.places(), [])
        self.assertFalse(ovh.paid())

    def test_stops_a_target_whose_price_is_above_its_cap_without_paying(self):
        ovh = FakeOvh(price=13.0)
        self.add(ovh)
        self.watch(ovh).run()
        self.assertIn("price above cap", (self.root / "stopped-a.json").read_text())
        self.assertFalse(ovh.paid())

    def test_keeps_waiting_when_the_stock_is_gone_at_checkout(self):
        ovh = FakeOvh(gone_at_checkout=True)
        self.add(ovh)
        self.watch(ovh).run()
        self.assertEqual(sorted(p.name for p in self.root.glob("*-a.json")), [])

    def test_orders_each_target_once(self):
        ovh = FakeOvh()
        self.add(ovh)
        self.watch(ovh).run()
        self.watch(ovh).run()
        self.assertEqual(ovh.places(), ["SGP"])

    def test_turns_the_timer_on_when_adding_and_off_once_every_target_is_finished(self):
        ovh = FakeOvh()
        self.add(ovh)
        self.watch(ovh).run()
        self.assertEqual(self.timer, [True, False])

    def test_adds_the_options_a_2027_plan_needs(self):
        self.add(FakeOvh())
        target = json.loads((self.root / "targets.json").read_text())["targets"][0]
        self.assertEqual(target["options"], ["option-storage-local-2027-model2", "option-auto-backup-2027-1-model2"])


if __name__ == "__main__":
    unittest.main()
