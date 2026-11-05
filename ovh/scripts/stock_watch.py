"""Watch OVH product stock and place one bounded order per target."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path


TIMER = os.environ.get("OVH_STOCK_WATCH_TIMER", "ovh-stock-watch.timer")
DEFAULT_OS = "Ubuntu 24.04"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def home():
    return Path(os.environ.get("OVH_STOCK_WATCH_HOME") or Path.home() / ".local/share/ovh-stock-watch")


def set_timer(on, timer_name=None):
    subprocess.run(["systemctl", "--user", "enable" if on else "disable", "--now", timer_name or TIMER],
                   check=False, capture_output=True)


def parse_places(text):
    places = [part.strip().upper() for part in text.split(",") if part.strip()]
    return ["any"] if places in ([], ["ANY"]) else places


def pick(stocked, places):
    if places == ["any"]:
        return stocked[0] if stocked else None
    return next((actual for wanted in places for actual in stocked if actual.upper() == wanted.upper()), None)


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Prices must be finite and nonnegative")
    return result


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class ApiError(RuntimeError):
    def __init__(self, method, path, status, result):
        self.status = status
        text = str(result).lower()
        self.stock_lost = status in (400, 409) and any(
            marker in text for marker in ("out of stock", "out-of-stock", "is not available in"))
        super().__init__(f"{method} {path.split('?')[0]} returned {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class Vps:
    product = "vps"

    def stock(self, watch, target, plan):
        query = urllib.parse.urlencode({"ovhSubsidiary": watch.state["subsidiary"], "planCode": plan})
        result = watch.step("GET", "/vps/order/rule/datacenter?" + query, signed=False)
        return [entry["datacenter"] for entry in result.get("datacenters", [])
                if entry.get("linuxStatus", entry.get("status")) == "available"]

    def options(self, target, plan):
        return target["options"]

    def configuration(self, target, plan, place):
        return {"vps_datacenter": place, "vps_os": target["os"]}


class Dedicated:
    product = "eco"

    def stock(self, watch, target, plan):
        query = urllib.parse.urlencode({"planCode": plan})
        result = watch.step("GET", "/dedicated/server/datacenter/availabilities?" + query, signed=False)
        places = []
        for row in result:
            if row.get("memory") != target["memory"] or row.get("storage") != target["storage"]:
                continue
            for entry in row.get("datacenters", []):
                available = entry.get("availability", "")
                if available == "available" or re.fullmatch(r"\d+[HMD]-(?:low|high)", available):
                    places.append(entry["datacenter"])
        return list(dict.fromkeys(places))

    def options(self, target, plan):
        selected = []
        for family in plan.get("addonFamilies", []):
            if not family.get("mandatory"):
                continue
            if family["name"] in ("memory", "storage"):
                matches = [code for code in family["addons"] if code.startswith(target[family["name"]] + "-")]
                if len(matches) != 1:
                    raise ValueError("The approved hardware option is missing or ambiguous")
                selected.append(matches[0])
            elif family.get("default"):
                selected.append(family["default"])
            else:
                raise ValueError("A mandatory option needs an explicit selection")
        if not all(any(code.startswith(target[kind] + "-") for code in selected)
                   for kind in ("memory", "storage")):
            raise ValueError("The approved memory and storage must both be selected")
        return selected

    def configuration(self, target, plan, place):
        available = {entry["name"]: entry.get("values", []) for entry in plan.get("configurations", [])}
        if place not in available.get("dedicated_datacenter", []):
            raise ValueError("Datacenter does not belong to this plan")
        if target["os"] not in available.get("dedicated_os", []):
            raise ValueError("The selected operating system is not offered")
        regions = available.get("region", [])
        region = "europe" if place not in ("bhs", "sgp", "syd") else "canada"
        if region not in regions:
            raise ValueError("The required region is not offered")
        return {"dedicated_datacenter": place, "dedicated_os": target["os"], "region": region}


PRODUCTS = {"vps": Vps(), "dedicated": Dedicated()}


class Watch:
    def __init__(self, root, http=None, timer=set_timer):
        self.root = Path(root)
        self.state = json.loads((self.root / "targets.json").read_text())
        self.http = http or self.request
        self.timer = timer
        self.time_offset = None
        self.opener = urllib.request.build_opener(NoRedirect())
        for target in self.state["targets"]:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", target["name"]):
                raise ValueError("Invalid target name")
            if target.get("kind", "vps") not in PRODUCTS:
                raise ValueError("Unsupported product kind")

    def save(self):
        atomic_json(self.root / "targets.json", self.state)

    def log(self, message):
        descriptor = os.open(self.root / "log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write(f"{now()} {message}\n")

    def result(self, target, key):
        return self.root / f"{key}-{target['name']}.json"

    def status(self, target):
        return next((key for key in ("ordered", "stopped", "pending") if self.result(target, key).exists()), "waiting")

    def done(self, target):
        return self.status(target) != "waiting"

    def finish(self, target, key, info):
        atomic_json(self.result(target, key), dict(info, at=now()))
        self.log(f"finished {target['name']}: {key} {json.dumps(info)}")
        if all(self.done(item) for item in self.state["targets"]):
            self.timer(False)

    def request(self, method, path, body=None, signed=True):
        base = (os.environ.get("OVH_API_BASE_URL") or self.state["api_base"]).rstrip("/")
        if base != self.state["api_base"].rstrip("/"):
            raise ValueError("The credential API endpoint differs from the configured account")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "https" or parsed.hostname not in ("ca.api.ovh.com", "eu.api.ovh.com", "api.us.ovhcloud.com"):
            raise ValueError("Unsupported OVH API endpoint")
        url, data = base + path, "" if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"}
        if signed:
            if self.time_offset is None:
                with self.opener.open(base + "/auth/time", timeout=20) as response:
                    self.time_offset = int(response.read().decode()) - int(time.time())
            stamp = str(int(time.time()) + self.time_offset)
            signature = "$1$" + hashlib.sha1("+".join([
                os.environ["OVH_APPLICATION_SECRET"], os.environ["OVH_ORDER_CONSUMER_KEY"],
                method, url, data, stamp]).encode()).hexdigest()
            headers.update({"X-Ovh-Application": os.environ["OVH_APPLICATION_KEY"],
                            "X-Ovh-Consumer": os.environ["OVH_ORDER_CONSUMER_KEY"],
                            "X-Ovh-Timestamp": stamp, "X-Ovh-Signature": signature})
        try:
            request = urllib.request.Request(url, data=data.encode() if body is not None else None,
                                             method=method, headers=headers)
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            return error.code, error.read(1000).decode()

    def step(self, method, path, body=None, signed=True):
        status, result = self.http(method, path, body, signed=signed)
        if status not in (200, 201, 204):
            raise ApiError(method, path, status, result)
        return result

    def candidates(self, target):
        adapter = PRODUCTS[target.get("kind", "vps")]
        found = []
        for plan in [target["plan"]] + target.get("alternate_plans", []):
            for place in adapter.stock(self, target, plan):
                if target["places"] == ["any"] or any(place.upper() == wanted.upper() for wanted in target["places"]):
                    found.append((plan, place))
        if target["places"] != ["any"]:
            found.sort(key=lambda pair: next(index for index, place in enumerate(target["places"])
                                             if place.upper() == pair[1].upper()))
        return found

    @staticmethod
    def rate(product, capacity):
        rates = [rate for rate in product.get("pricings", [])
                 if rate.get("mode") == "default" and capacity in rate.get("capacities", [])
                 and rate.get("commitment", 0) == 0
                 and (capacity != "renew" or (rate.get("interval") == 1 and rate.get("intervalUnit") == "month"))]
        if capacity == "installation" and not rates:
            return Decimal(0)
        if len(rates) != 1:
            raise ValueError("The uncommitted monthly price is missing or ambiguous")
        return money(rates[0]["price"]) / Decimal(100000000)

    def prepare(self, target, plan_code, place):
        adapter = PRODUCTS[target.get("kind", "vps")]
        query = urllib.parse.urlencode({"ovhSubsidiary": self.state["subsidiary"]})
        catalog = self.step("GET", f"/order/catalog/public/{adapter.product}?" + query, signed=False)
        currency = catalog["locale"]["currencyCode"]
        if target.get("currency", currency) != currency:
            raise ValueError("The catalog currency differs from the approved currency")
        plan = next(item for item in catalog["plans"] if item["planCode"] == plan_code)
        option_codes = adapter.options(target, plan)
        addons = {item["planCode"]: item for item in catalog["addons"]}
        products = [plan] + [addons[code] for code in option_codes]
        monthly = sum((self.rate(item, "renew") for item in products), Decimal(0))
        first = monthly + sum((self.rate(item, "installation") for item in products), Decimal(0))
        for amount, cap in ((monthly, target.get("max_monthly_without_tax", target["max_monthly_price"])),
                            (first, target.get("max_first_without_tax", target.get("max_first_price", target["max_monthly_price"])))):
            if amount > money(cap):
                raise ValueError("price above cap")
        configurations = adapter.configuration(target, plan, place)
        cart = self.step("POST", "/order/cart", {"ovhSubsidiary": self.state["subsidiary"],
                                                   "description": target["description"]})["cartId"]
        atomic_json(self.result(target, "cart"), {"cart_id": cart, "plan": plan_code, "place": place, "at": now()})
        self.step("POST", f"/order/cart/{cart}/assign")
        item = self.step("POST", f"/order/cart/{cart}/{adapter.product}",
                         {"planCode": plan_code, "duration": "P1M", "pricingMode": "default", "quantity": 1})["itemId"]
        for label, value in configurations.items():
            self.step("POST", f"/order/cart/{cart}/item/{item}/configuration", {"label": label, "value": value})
        for code in option_codes:
            self.step("POST", f"/order/cart/{cart}/{adapter.product}/options",
                      {"itemId": item, "planCode": code, "duration": "P1M", "pricingMode": "default", "quantity": 1})
        required = self.step("GET", f"/order/cart/{cart}/item/{item}/requiredConfiguration") or []
        if any(entry.get("required") and entry["label"] not in configurations for entry in required):
            raise ValueError("A required configuration has not been selected")
        quote = self.step("GET", f"/order/cart/{cart}/checkout")
        prices = quote["prices"]
        if any(prices[key]["currencyCode"] != currency for key in ("withTax", "withoutTax")):
            raise ValueError("Checkout currency differs from the approved currency")
        before, total = money(prices["withoutTax"]["value"]), money(prices["withTax"]["value"])
        if abs(before - first) > Decimal("0.01") or total < before:
            raise ValueError("Checkout prices differ from the catalog")
        if before == 0 and monthly != 0:
            raise ValueError("The recurring tax-inclusive price cannot be determined")
        recurring = (monthly * total / before if before else Decimal(0)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        if total > money(target.get("max_first_price", target["max_monthly_price"])) or recurring > money(target["max_monthly_price"]):
            raise ValueError("price above cap")
        info = {"cart_id": cart, "plan": plan_code, "place": place, "currency": currency,
                "first_price": str(total), "monthly_price": str(recurring)}
        atomic_json(self.result(target, "quote"), dict(info, at=now()))
        return info

    def order(self, target, plan, place):
        if self.done(target):
            return
        pending = self.result(target, "pending")
        try:
            info = self.prepare(target, plan, place)
            current = json.loads((self.root / "targets.json").read_text())
            if target not in current["targets"]:
                raise ValueError("Purchase authorization changed while preparing the order")
            atomic_json(pending, dict(info, at=now()))
            placed = self.step("POST", f"/order/cart/{info['cart_id']}/checkout",
                               {"autoPayWithPreferredPaymentMethod": True, "waiveRetractationPeriod": True})
            if not isinstance(placed, dict) or type(placed.get("orderId")) is not int:
                raise ValueError("Checkout returned no order identifier; reconciliation is required")
            self.finish(target, "ordered", dict(info, orderId=placed["orderId"]))
            pending.unlink(missing_ok=True)
        except ApiError as error:
            if error.stock_lost:
                pending.unlink(missing_ok=True)
                self.log(f"{target['name']}: stock was lost while preparing the order; waiting")
            elif not pending.exists() and error.status in (408, 429, 500, 502, 503, 504):
                self.log(f"{target['name']}: temporary provider error before checkout; waiting")
            else:
                self.finish(target, "stopped", {"reason": str(error), "outcome_uncertain": pending.exists()})
        except (OSError, TimeoutError) as error:
            if pending.exists():
                self.finish(target, "stopped", {"reason": type(error).__name__, "outcome_uncertain": True})
            else:
                self.log(f"{target['name']}: temporary network error before checkout; waiting")
        except Exception as error:
            self.finish(target, "stopped", {"reason": str(error)[:200], "outcome_uncertain": pending.exists()})

    def waiting(self):
        return [target for target in self.state["targets"] if not self.done(target)]

    def run(self):
        descriptor = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self.state = json.loads((self.root / "targets.json").read_text())
            if not self.waiting():
                self.timer(False)
                return
            for target in self.waiting():
                try:
                    candidates = self.candidates(target)
                except (ApiError, OSError, TimeoutError) as error:
                    self.log(f"{target['name']}: stock check failed: {type(error).__name__}")
                    continue
                self.log(f"{target['name']}: matching stock in {','.join(place for _, place in candidates) or 'no place'}")
                if candidates:
                    self.order(target, *candidates[0])

    def check(self):
        for target in self.waiting():
            candidates = self.candidates(target)
            print(json.dumps({"name": target["name"], "candidates": candidates}))

    def listing(self):
        for target in self.state["targets"]:
            print(json.dumps({"name": target["name"], "status": self.status(target),
                              "kind": target.get("kind", "vps"), "plan": target["plan"],
                              "max_monthly_price": target["max_monthly_price"],
                              "max_first_price": target.get("max_first_price", target["max_monthly_price"])}))

    def add(self, name, plan, max_price, places, os_name, options, description, **extra):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", name):
            raise SystemExit("A name uses lower-case letters, digits and hyphens.")
        if any(target["name"] == name for target in self.state["targets"]):
            raise SystemExit(f"{name} is already a target.")
        kind = extra.pop("kind", "vps")
        if kind == "vps" and options is None:
            model = re.fullmatch(r"vps-2027-model(\d+)", plan)
            if not model:
                raise SystemExit("Give --options for a VPS plan other than vps-2027-modelN.")
            options = [f"option-storage-local-2027-model{model[1]}", f"option-auto-backup-2027-1-model{model[1]}"]
        if kind == "dedicated" and not all(extra.get(key) for key in ("memory", "storage", "max_first_price")):
            raise SystemExit("Dedicated servers require memory, storage and a first-payment cap.")
        money(max_price)
        for key in ("max_first_price", "max_monthly_without_tax", "max_first_without_tax"):
            if key in extra:
                money(extra[key])
        target = dict(name=name, kind=kind, plan=plan, max_monthly_price=max_price, places=places,
                      os=os_name, options=options or [], description=description or name, **extra)
        self.state["targets"].append(target)
        self.save()
        self.log(f"added {name}: {kind} {plan}")
        self.timer(True)

    def remove(self, name):
        target = next((target for target in self.state["targets"] if target["name"] == name), None)
        if target is None or self.done(target):
            raise SystemExit("Only a waiting target can be removed.")
        self.state["targets"].remove(target)
        self.save()
        if all(self.done(item) for item in self.state["targets"]):
            self.timer(False)


def main(argv=None, root=None, timer=set_timer):
    parser = argparse.ArgumentParser(description="Watch OVH VPS or dedicated server stock and order each target once.")
    parser.add_argument("--home", type=Path, help="private directory containing this watch profile")
    parser.add_argument("--timer", help="user timer belonging to this watch profile")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="register one approved purchase")
    add.add_argument("name")
    add.add_argument("plan")
    add.add_argument("max_price", help="monthly cap with tax in the account currency")
    add.add_argument("--kind", choices=PRODUCTS, default="vps")
    add.add_argument("--places", default="any")
    add.add_argument("--os")
    add.add_argument("--options")
    add.add_argument("--description")
    add.add_argument("--memory")
    add.add_argument("--storage")
    add.add_argument("--max-first-price")
    add.add_argument("--max-monthly-without-tax")
    add.add_argument("--max-first-without-tax")
    add.add_argument("--currency")
    add.add_argument("--alternate-plans")
    remove = commands.add_parser("remove", help="stop waiting for a target")
    remove.add_argument("name")
    commands.add_parser("list", help="show registered targets")
    commands.add_parser("check", help="check matching stock without ordering")
    commands.add_parser("run", help="place approved orders when matching stock appears")
    args = parser.parse_args(argv)
    if args.timer and not re.fullmatch(r"[A-Za-z0-9_.@-]+\.timer", args.timer):
        parser.error("--timer must name a user timer")
    timer_callback = (lambda on: set_timer(on, args.timer)) if args.timer else timer
    watch = Watch(root or args.home or home(), timer=timer_callback)
    if args.command == "add":
        extra = {key: getattr(args, key) for key in ("memory", "storage", "max_first_price", "max_monthly_without_tax",
                 "max_first_without_tax", "currency") if getattr(args, key) is not None}
        extra["kind"] = args.kind
        if args.alternate_plans:
            extra["alternate_plans"] = [part.strip() for part in args.alternate_plans.split(",") if part.strip()]
        watch.add(args.name, args.plan, args.max_price, parse_places(args.places),
                  args.os or ("none_64.en" if args.kind == "dedicated" else DEFAULT_OS),
                  args.options.split(",") if args.options is not None else None, args.description, **extra)
    elif args.command == "remove":
        watch.remove(args.name)
    else:
        {"list": watch.listing, "check": watch.check, "run": watch.run}[args.command]()


if __name__ == "__main__":
    main()
