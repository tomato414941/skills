"""Wait for OVHcloud VPS stock and order each waited-for VPS once.

Each target names a VPS plan, the places it may go (or any place), a monthly price cap with tax and
its options. `run` checks the Linux stock of every waiting target and orders it in the first allowed
place that has Linux stock, paying automatically. A target is finished after one order, after an
unexpected error or when its price is above its cap, so it is never ordered twice. A place can show
`status: available` while only Windows images are in stock, so this reads `linuxStatus`.

The state (targets.json, the log and one result file per finished target) lives in
$OVH_VPS_WATCH_HOME, by default ~/.local/share/ovh-vps-watch. targets.json also holds the account's
`subsidiary` and `api_base`. `run` signs its calls with OVH_APPLICATION_KEY, OVH_APPLICATION_SECRET
and OVH_ORDER_CONSUMER_KEY from the environment; the other commands need no keys.
"""
import argparse, hashlib, json, os, re, subprocess, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMER = "ovh-vps-watch.timer"
DEFAULT_OS = "Ubuntu 24.04"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def home():
    return Path(os.environ.get("OVH_VPS_WATCH_HOME") or Path.home() / ".local/share/ovh-vps-watch")


def set_timer(on):
    subprocess.run(["systemctl", "--user", "enable" if on else "disable", "--now", TIMER],
                   check=False, capture_output=True)


def parse_places(text):
    places = [p.strip().upper() for p in text.split(",") if p.strip()]
    return ["any"] if places in ([], ["ANY"]) else places


def pick(stocked, places):
    """The first allowed place with Linux stock, in the target's order of preference."""
    if places == ["any"]:
        return stocked[0] if stocked else None
    return next((p for p in places if p in stocked), None)


class Watch:
    def __init__(self, root, http=None, timer=set_timer):
        self.root = Path(root)
        self.state = json.loads((self.root / "targets.json").read_text())
        self.http = http or self.request
        self.timer = timer

    def save(self):
        path = self.root / "targets.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n")
        temporary.replace(path)

    def log(self, message):
        with (self.root / "log").open("a") as f:
            f.write(f"{now()} {message}\n")

    def result(self, target, key):
        return self.root / f"{key}-{target['name']}.json"

    def done(self, target):
        return self.result(target, "ordered").exists() or self.result(target, "stopped").exists()

    def finish(self, target, key, info):
        path = self.result(target, key)
        path.write_text(json.dumps(dict(info, at=now()), indent=2) + "\n")
        self.log(f"finished {target['name']}: {path.name} {json.dumps(info)}")
        if all(self.done(t) for t in self.state["targets"]):
            self.timer(False)

    def request(self, method, path, body=None, signed=True):
        base = (os.environ.get("OVH_API_BASE_URL") or self.state["api_base"]).rstrip("/")
        url, data = base + path, ("" if body is None else json.dumps(body))
        headers = {"Content-Type": "application/json"}
        if signed:
            secret, consumer = os.environ["OVH_APPLICATION_SECRET"], os.environ["OVH_ORDER_CONSUMER_KEY"]
            with urllib.request.urlopen(base + "/auth/time", timeout=30) as r:
                stamp = r.read().decode().strip()
            signature = "$1$" + hashlib.sha1("+".join([secret, consumer, method, url, data, stamp]).encode()).hexdigest()
            headers.update({"X-Ovh-Application": os.environ["OVH_APPLICATION_KEY"], "X-Ovh-Consumer": consumer,
                            "X-Ovh-Timestamp": stamp, "X-Ovh-Signature": signature})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data.encode() or None, method=method,
                                                               headers=headers), timeout=60) as r:
                raw = r.read().decode()
                return 200, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:500]

    def step(self, method, path, body=None):
        code, result = self.http(method, path, body)
        if code == 200:
            return result
        if "out of stock" in str(result).lower():
            raise LookupError(str(result))
        raise RuntimeError(f"{method} {path.split('?')[0]} -> {code} {result}")

    def linux_stock(self, plan):
        """The places with Linux stock for a plan, or None when the check fails."""
        code, result = self.http("GET", f"/vps/order/rule/datacenter?ovhSubsidiary={self.state['subsidiary']}"
                                        f"&planCode={plan}", signed=False)
        if code != 200:
            self.log(f"stock check failed for {plan}: {code} {result}")
            return None
        return [d["datacenter"] for d in result.get("datacenters", [])
                if d.get("linuxStatus", d.get("status")) == "available"]

    def order(self, target, place):
        self.log(f"{target['name']}: ordering {target['plan']} in {place}")
        try:
            cart = self.step("POST", "/order/cart", {"ovhSubsidiary": self.state["subsidiary"],
                                                     "description": target["description"]})["cartId"]
            self.step("POST", f"/order/cart/{cart}/assign")
            item = self.step("POST", f"/order/cart/{cart}/vps", {"planCode": target["plan"], "duration": "P1M",
                                                                 "pricingMode": "default", "quantity": 1})["itemId"]
            for label, value in (("vps_datacenter", place), ("vps_os", target["os"])):
                self.step("POST", f"/order/cart/{cart}/item/{item}/configuration", {"label": label, "value": value})
            for option in target["options"]:
                self.step("POST", f"/order/cart/{cart}/vps/options", {"itemId": item, "planCode": option,
                                                                      "duration": "P1M", "pricingMode": "default",
                                                                      "quantity": 1})
            price = float(self.step("GET", f"/order/cart/{cart}/checkout")["prices"]["withTax"]["value"])
            if price > target["max_monthly_price"]:
                self.finish(target, "stopped", {"reason": "price above cap", "place": place, "monthly_price": price})
                return
            placed = self.step("POST", f"/order/cart/{cart}/checkout",
                               {"autoPayWithPreferredPaymentMethod": True, "waiveRetractationPeriod": True})
            self.finish(target, "ordered", {"orderId": placed.get("orderId"), "place": place, "monthly_price": price})
        except LookupError as e:
            self.log(f"{target['name']}: out of stock again while ordering in {place}: {e}")
        except Exception as e:
            self.finish(target, "stopped", {"reason": "unexpected error", "place": place, "error": str(e)[:300]})

    def waiting(self):
        return [t for t in self.state["targets"] if not self.done(t)]

    def run(self):
        stock = {}
        for target in self.waiting():
            plan = target["plan"]
            if plan not in stock:
                stock[plan] = self.linux_stock(plan)
                if stock[plan] is not None:
                    self.log(f"{plan} Linux stock: {', '.join(stock[plan]) or 'none'}")
            place = pick(stock[plan] or [], target["places"])
            if place:
                self.order(target, place)

    def check(self):
        stock = {}
        for target in self.waiting():
            plan = target["plan"]
            if plan not in stock:
                stock[plan] = self.linux_stock(plan) or []
            place = pick(stock[plan], target["places"])
            print(f"{target['name']}: Linux stock in {', '.join(stock[plan]) or 'no place'}; "
                  f"{'would order in ' + place if place else 'would wait'}")

    def listing(self):
        for t in self.state["targets"]:
            state = ("ordered" if self.result(t, "ordered").exists() else
                     "stopped" if self.result(t, "stopped").exists() else "waiting")
            print(f"{t['name']}: {state}, {t['plan']} in {','.join(t['places'])}, "
                  f"up to {t['max_monthly_price']}/month with tax")

    def add(self, name, plan, max_price, places, os_name, options, description):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise SystemExit("A name uses lower-case letters, digits and hyphens.")
        if any(t["name"] == name for t in self.state["targets"]):
            raise SystemExit(f"{name} is already a target.")
        if options is None:
            model = re.fullmatch(r"vps-2027-model(\d+)", plan)
            if not model:
                raise SystemExit("Give --options for a plan other than vps-2027-modelN.")
            options = [f"option-storage-local-2027-model{model[1]}", f"option-auto-backup-2027-1-model{model[1]}"]
        self.state["targets"].append({"name": name, "plan": plan, "max_monthly_price": max_price, "places": places,
                                      "os": os_name, "options": options, "description": description or name})
        self.save()
        self.log(f"added {name}: {plan} in {','.join(places)}, up to {max_price}/month")
        self.timer(True)

    def remove(self, name):
        target = next((t for t in self.state["targets"] if t["name"] == name), None)
        if target is None:
            raise SystemExit(f"{name} is not a target.")
        if self.done(target):
            raise SystemExit(f"{name} is finished; its result file records what happened.")
        self.state["targets"].remove(target)
        self.save()
        self.log(f"removed {name}")
        if all(self.done(t) for t in self.state["targets"]):
            self.timer(False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Wait for OVHcloud VPS stock and order each waited-for VPS once.")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="wait for one more VPS")
    add.add_argument("name")
    add.add_argument("plan", help="plan code, such as vps-2027-model2")
    add.add_argument("max_price", type=float, help="monthly price cap with tax, in the account's currency")
    add.add_argument("--places", default="any", help="'any', or datacenter codes such as SGP,GRA in order of preference")
    add.add_argument("--os", default=DEFAULT_OS)
    add.add_argument("--options", help="comma-separated option plan codes; derived for vps-2027-modelN")
    add.add_argument("--description")
    remove = commands.add_parser("remove", help="stop waiting for a VPS")
    remove.add_argument("name")
    commands.add_parser("list", help="show every target and its state")
    commands.add_parser("check", help="show where each waiting target could be ordered now, without ordering")
    commands.add_parser("run", help="order every waiting target that has Linux stock (the timer runs this)")
    args = parser.parse_args(argv)
    watch = Watch(home())
    if args.command == "add":
        options = None if args.options is None else [o for o in args.options.split(",") if o]
        watch.add(args.name, args.plan, args.max_price, parse_places(args.places), args.os, options, args.description)
    elif args.command == "remove":
        watch.remove(args.name)
    else:
        {"list": watch.listing, "check": watch.check, "run": watch.run}[args.command]()


if __name__ == "__main__":
    main()
