"""Rent GPU machines on Vast.ai, check each one in its first minutes, and keep a ledger of how every machine did.

Every rental goes in one ledger that all sessions on this workstation share (~/.local/share/gpu-rental/ledger.jsonl,
or $GPU_RENTAL_HOME): what was offered, what the check measured, how a machine failed, when it went back, and what the
work noted of it. A search leaves out machines that failed once and offers whose power limit is below the default that
checked machines with the same GPU reported, and puts hosts that failed before after the others. A rented machine runs
machine_check.py at once and goes back if its GPUs hold memory, if its power is capped below its default, or if it is
slower than the median of the checked machines with the same GPU. Machines with different GPUs are never compared.
Each rental gets a guard that returns the machine at its deadline, even if the session that rented it has ended.

Calls to Vast.ai need VAST_API_KEY: commands that make them run the script again under Foundation, which delivers the
key. SSH goes through an agent that Foundation fills with the tool's key for each connection. ~/.config/gpu-rental/
config.json (or $GPU_RENTAL_CONFIG) names both, and the public half of the SSH key that rented machines accept:
  {"vast_api_key": {"id": "<Foundation item>", "output": "VAST_API_KEY"}, "ssh_key": "<Foundation SSH key>",
   "ssh_public_key": "<that key's public half>"}
Nothing here prints an address or a key.

Usage:
  gpu_rental.py offers GPU [--count N] [--max-price DOLLARS] [--countries CC,CC] [--disk GB] [--query JSON] [--show N]
  gpu_rental.py rent GPU --image IMAGE --label LABEL --hours HOURS [the options of offers] [--tries N]
                [--min-download MBPS] [--env JSON] [--onstart COMMAND]
  gpu_rental.py run INSTANCE [--timeout SECONDS] -- COMMAND
  gpu_rental.py extend INSTANCE HOURS
  gpu_rental.py note INSTANCE TEXT [--values JSON]
  gpu_rental.py return INSTANCE
  gpu_rental.py ledger [GPU]
"""

import argparse
import contextlib
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import machine_check

HERE = Path(__file__).resolve().parent
API = os.environ.get("VAST_URL", "https://console.vast.ai").rstrip("/") + "/api/v0"
PROVIDER = "vast"
# A GPU that holds this much memory before the work starts is running someone else's.
BUSY_MIB = 1024
# A machine slower than this share of the median of the checked machines with its GPU goes back.
SLOW_SHARE = 0.9
# The fewest checked machines with the same GPU whose median a new machine is held to.
FEWEST_TO_COMPARE = 2
SPEEDS = ("matmul_tflops", "memory_gbps")
START_SECONDS = 20 * 60
SSH_SECONDS = 4 * 60
CHECK_SECONDS = 5 * 60
GUARD_POLL_SECONDS = 60
GUARD_START_SECONDS = 90
GUARD_LIFETIME_SECONDS = 8 * 24 * 3600
RETRYABLE = {429, 502, 503, 504}
# Words of a container's status that blame the image, not the machine.
IMAGE_FAULTS = ("manifest unknown", "pull access denied", "repository does not exist", "invalid reference format")
# The commands that call Vast.ai with the key.
KEYED = {"rent", "run", "return", "guard"}


class VastError(Exception):
    def __init__(self, code, message):
        super().__init__(f"Vast.ai answered {code}: {message}")
        self.code = code


class ImageFault(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def stamp(moment=None):
    return (moment or now()).isoformat(timespec="seconds")


def home():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return Path(os.environ.get("GPU_RENTAL_HOME") or data / "gpu-rental")


def config_path():
    settings = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gpu-rental" / "config.json"
    return Path(os.environ.get("GPU_RENTAL_CONFIG") or settings)


def config():
    path = config_path()
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"{path} is missing; the docstring of {Path(__file__).name} says what it holds") from None


def hide_addresses(text):
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", text)
    text = re.sub(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}\b", "<ip>", text)
    return re.sub(r"\b[\w-]+\.vast\.ai\b", "<host>", text)


# The ledger


def record(event, **fields):
    entry = {"at": stamp(), "event": event, **fields}
    path = home() / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # One write of one line to a file opened for appending, so that the lines of concurrent writers do not mix.
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def entries():
    try:
        lines = (home() / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    found = []
    for line in lines:
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:  # a line that a writer left unfinished
            continue
    return found


def avoided(ledger):
    """The machines that failed once: they did not start, could not be reached or failed their check."""
    return {(entry["provider"], entry["machine"]) for entry in ledger if entry["event"] == "unusable"}


def failing_hosts(ledger):
    return Counter((entry["provider"], entry["host"]) for entry in ledger
                   if entry["event"] == "unusable" and entry.get("host") is not None)


def typical(ledger, gpu, version):
    """The median of each speed over the machines with this GPU that passed a check of this version, and their number."""
    checks = [entry["results"] for entry in ledger if entry["event"] == "checked" and entry.get("kept")
              and entry.get("gpu") == gpu and entry["results"].get("version") == version]
    medians = {}
    for name in SPEEDS:
        values = [check[name] for check in checks if check.get(name)]
        if values:
            medians[name] = statistics.median(values)
    return medians, len(checks)


def default_power(ledger, gpu):
    """The default power limit that checked machines with this GPU reported, or None before the first check."""
    values = [row["power_default_w"] for entry in ledger if entry["event"] == "checked" and entry.get("gpu") == gpu
              for row in entry["results"].get("gpus") or [] if row.get("power_default_w")]
    return statistics.median(values) if values else None


def choose(offers, ledger):
    """The offers to try, in order: without the machines that failed once, nor offers whose power limit is below the
    default of checked machines with the same GPU; hosts that failed before after the rest; each group cheapest first."""
    left_out, failures, defaults, kept = avoided(ledger), failing_hosts(ledger), {}, []
    for offer in offers:
        if (PROVIDER, offer["machine_id"]) in left_out:
            continue
        gpu = offer["gpu_name"]
        if gpu not in defaults:
            defaults[gpu] = default_power(ledger, gpu)
        limit = offer.get("gpu_max_power")
        if defaults[gpu] and limit and limit < defaults[gpu] - 1:
            continue
        kept.append(offer)
    return sorted(kept, key=lambda offer: (failures[(PROVIDER, offer["host_id"])] > 0, offer["dph_total"]))


def verdict(results, ledger, gpu, min_download_mbps=0):
    """Why a checked machine should go back, if at all: GPUs that hold memory, power capped below its default, speeds
    below SLOW_SHARE of the median of checked machines with the same GPU, or a download slower than the work needs."""
    reasons = []
    rows = results.get("gpus") or []
    if not rows:
        reasons.append("no_gpu: nvidia-smi reported no GPU")
    for index, row in enumerate(rows):
        if (row.get("memory_used_mib") or 0) >= BUSY_MIB:
            reasons.append(f"gpu_busy: GPU {index} holds {row['memory_used_mib']:.0f} MiB")
        limit, default = row.get("power_limit_w"), row.get("power_default_w")
        if limit and default and limit < default - 1:
            reasons.append(f"power_capped: GPU {index} is limited to {limit:.0f} of its default {default:.0f} W")
    medians, count = typical(ledger, gpu, results.get("version"))
    if count >= FEWEST_TO_COMPARE:
        for name, median in medians.items():
            value = results.get(name)
            if value is not None and value < SLOW_SHARE * median:
                reasons.append(f"slow: {name} {value:.0f} against a median of {median:.0f} over {count} machines")
    download = results.get("download_mbps") or 0
    if min_download_mbps and download < min_download_mbps:
        reasons.append(f"slow_download: {download:.0f} MB/s against the {min_download_mbps:.0f} needed")
    return reasons


def rental(ledger, instance):
    """The ledger's record of a rental, with its latest deadline and whether it went back; None if it has none."""
    held = None
    for entry in ledger:
        if entry.get("instance") != instance or entry.get("provider") != PROVIDER:
            continue
        if entry["event"] == "rented":
            held = {**entry, "returned": False}
        elif held is not None and entry["event"] == "extended":
            held["deadline"] = entry["deadline"]
        elif held is not None and entry["event"] == "returned":
            held["returned"] = True
    return held


# Vast.ai


def call(method, path, body=None, query=None):
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode({key: value if isinstance(value, str) else json.dumps(value)
                                             for key, value in query.items()})
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "gpu-rental"}
    if os.environ.get("VAST_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["VAST_API_KEY"]
    data = None if body is None else json.dumps(body).encode()
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE or attempt == 3:
                raise VastError(error.code, hide_addresses(error.read().decode(errors="replace"))[:300]) from None
        except urllib.error.URLError:
            if attempt == 3:
                raise
        time.sleep(5 * 2**attempt)


def search(gpu, count=1, disk=60, query=None, limit=200):
    filters = {"gpu_name": {"eq": gpu}, "num_gpus": {"eq": count}, "rentable": {"eq": True}, "rented": {"eq": False},
               "external": {"eq": False}, "disk_space": {"gte": disk}, **(query or {})}
    found = call("GET", "/bundles/", query={"q": {**filters, "order": [["dph_total", "asc"]], "type": "on-demand",
                                                  "limit": limit, "allocated_storage": disk}})
    # Hosts that lost their verification have not started a container for us.
    return [offer for offer in found["offers"] if offer.get("verification") != "deverified"]


def create(offer, image, disk, label, env=None, onstart=None):
    body = {"client_id": "me", "image": image, "env": env or {}, "price": None, "disk": disk, "label": label,
            "extra": None, "onstart": onstart, "image_login": None, "python_utf8": False, "lang_utf8": False,
            "use_jupyter_lab": False, "jupyter_dir": None, "force": False, "cancel_unavail": True,
            "template_hash_id": None, "user": None, "runtype": "ssh_direc ssh_proxy"}
    return call("PUT", f"/asks/{offer['id']}/", body)["new_contract"]


def show(instance):
    """The instance as Vast.ai lists it, or None once it is gone."""
    try:
        return call("GET", f"/instances/{instance}/", query={"owner": "me"})["instances"]
    except VastError as error:
        if error.code == 404:
            return None
        raise


def destroy(instance):
    return call("DELETE", f"/instances/{instance}/", {})


def attach(instance, public_key):
    return call("POST", f"/instances/{instance}/ssh/", {"ssh_key": public_key})


# SSH


def routes(row):
    """Where the instance's SSH answers: the host's own port first, as `vastai ssh-url` prefers, then Vast.ai's proxy."""
    mapped = (row.get("ports") or {}).get("22/tcp") or []
    found = [(row["public_ipaddr"], mapped[0]["HostPort"])] if mapped and row.get("public_ipaddr") else []
    if row.get("ssh_host") and row.get("ssh_port"):
        found.append((row["ssh_host"], row["ssh_port"]))
    return found


def ssh_base(instance, host, port):
    """ssh to the instance by one route, inside an agent that Foundation fills with the tool's key."""
    known = home() / "known_hosts"
    known.mkdir(parents=True, exist_ok=True)
    return ["foundation", "ssh-key", "agent", config()["ssh_key"], "--",
            "ssh", "-p", str(port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "LogLevel=ERROR",
            "-o", "ServerAliveInterval=30", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known / str(instance)}", f"root@{host}"]


def ssh_command(instance):
    """The ssh command for the instance, without the command to run there, by the first route that answers; None if
    none does. Code that streams data to or from a machine runs this command itself."""
    for host, port in routes(show(instance) or {}):
        base = ssh_base(instance, host, port)
        try:
            if subprocess.run([*base, "true"], capture_output=True, timeout=90).returncode == 0:
                return base
        except subprocess.TimeoutExpired:
            continue
    return None


def wait_until_running(instance):
    """None once the container runs, or what its last status said."""
    deadline, row = time.monotonic() + START_SECONDS, {}
    while time.monotonic() < deadline:
        time.sleep(30)
        try:
            row = show(instance) or {}
        except (VastError, OSError):
            continue
        if row.get("actual_status") == "running":
            return None
        message = (row.get("status_msg") or "").lower()
        if any(fault in message for fault in IMAGE_FAULTS):
            raise ImageFault(hide_addresses(row.get("status_msg") or ""))
        if row.get("intended_status") == "stopped" or "error" in message:
            break
    return f"{row.get('actual_status')}: {hide_addresses(row.get('status_msg') or '')}"


def wait_for_ssh(instance):
    deadline = time.monotonic() + SSH_SECONDS
    while time.monotonic() < deadline:
        try:
            base = ssh_command(instance)
        except (VastError, OSError):
            base = None
        if base:
            return base
        time.sleep(20)
    return None


def check(base):
    """machine_check.py's results on the machine, or None and why it did not run."""
    try:
        done = subprocess.run([*base, "python3 -"], input=(HERE / "machine_check.py").read_text(), capture_output=True,
                              text=True, timeout=CHECK_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"the check ran longer than {CHECK_SECONDS} seconds"
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if done.returncode or not lines:
        return None, f"the check failed with {done.returncode}: {hide_addresses(done.stderr)[-300:]}"
    return json.loads(lines[-1]), None


# Rentals


def identity(offer):
    return {"provider": PROVIDER, "machine": offer["machine_id"], "host": offer["host_id"], "gpu": offer["gpu_name"],
            "count": offer["num_gpus"], "where": offer.get("geolocation")}


def finish(instance, by):
    """Destroy the instance and record that it went back, with its hours and their cost at its price. Raise if it is
    still there, so that its guard keeps watching it."""
    try:
        answer = destroy(instance)
        destroyed = not isinstance(answer, dict) or answer.get("success", True) is not False
    except VastError:
        destroyed = False
    if not destroyed and show(instance) is not None:
        raise VastError(None, f"instance {instance} could not be destroyed")
    held = rental(entries(), instance)
    hours = (now() - datetime.fromisoformat(held["at"])).total_seconds() / 3600
    return record("returned", provider=PROVIDER, instance=instance, by=by, hours=round(hours, 3),
                  dollars=round(hours * (held.get("price") or 0), 3))


def start_guard(instance):
    """Start the guard in a session of its own, so that it outlives this command, and wait until it records itself;
    a guard that does not is stopped with all it started."""
    logs = home() / "guards"
    logs.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key != "VAST_API_KEY"}
    command = [sys.executable, str(Path(__file__).resolve()), "--home", str(home()), "--config", str(config_path()),
               "guard", str(instance)]
    with open(logs / f"{instance}.log", "a") as log:
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
    deadline = time.monotonic() + GUARD_START_SECONDS
    while time.monotonic() < deadline:
        if any(entry["event"] == "guarding" and entry.get("instance") == instance for entry in entries()):
            return True
        time.sleep(3)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    return False


def guard(instance, poll=GUARD_POLL_SECONDS):
    """Return the machine at its latest deadline, unless it went back before."""
    record("guarding", provider=PROVIDER, instance=instance, pid=os.getpid())
    while True:
        held = rental(entries(), instance)
        if held is None or held["returned"]:
            return 0
        if now() >= datetime.fromisoformat(held["deadline"]):
            try:
                finish(instance, by="guard")
                return 0
            except (VastError, OSError) as error:
                print(stamp(), "could not return the machine:", hide_addresses(str(error)), flush=True)
        time.sleep(poll)


def matches(offer, max_price=None, countries=None):
    country = (offer.get("geolocation") or "").rsplit(", ", 1)[-1]
    return (max_price is None or offer["dph_total"] <= max_price) and (not countries or country in countries)


def summary(results):
    names = ("matmul_tflops", "memory_gbps", "sm_clock_under_load_mhz", "power_under_load_w", "host_to_gpu_gbps",
             "download_mbps", "disk_write_mbps")
    found = {name: round(results[name], 1) for name in names if isinstance(results.get(name), (int, float))}
    found["power_limit_w"] = [row.get("power_limit_w") for row in results.get("gpus") or []]
    return found


def rent_one(offer, args, public_key):
    """Rent the offer and check its machine: "kept" with the instance and the check's results if it passed, "failed"
    if it went back, or "taken" if someone rented the offer first."""
    try:
        instance = create(offer, args.image, args.disk, args.label, args.env, args.onstart)
    except VastError as error:
        if error.code in (401, 403):
            raise
        return "taken", None
    deadline = stamp(now() + timedelta(hours=args.hours))
    record("rented", **identity(offer), instance=instance, offer=offer["id"], price=offer["dph_total"],
           power_limit_w=offer.get("gpu_max_power"), reliability=offer.get("reliability2"),
           verification=offer.get("verification"), image=args.image, label=args.label, deadline=deadline)
    try:
        if not start_guard(instance):
            raise RuntimeError("the guard did not start")
        stage, problem = "start", wait_until_running(instance)
        if problem is None:
            attach(instance, public_key)
            base = wait_for_ssh(instance)
            stage, problem = ("ssh", "no route accepted ssh") if base is None else ("check", None)
        results = None
        if problem is None:
            results, failure = check(base)
            if results is None:
                # A check that does not run says more of the image than of the machine.
                raise RuntimeError(failure)
            reasons = verdict(results, entries(), offer["gpu_name"], args.min_download)
            record("checked", **identity(offer), instance=instance, results=results, kept=not reasons, reasons=reasons)
            problem = "; ".join(reasons) or None
    except BaseException:
        finish(instance, by="error")
        raise
    if problem is not None:
        record("unusable", **identity(offer), instance=instance, stage=stage, detail=problem[-500:])
        finish(instance, by=stage)
        print(json.dumps({"unusable": instance, "machine": offer["machine_id"], "stage": stage, "detail": problem}),
              flush=True)
        return "failed", None
    return "kept", (instance, results)


# Commands


def list_offers(args):
    ledger = entries()
    offers = [offer for offer in choose(search(args.gpu, args.count, args.disk, args.query), ledger)
              if matches(offer, args.max_price, args.countries)]
    failures = failing_hosts(ledger)
    for offer in offers[:args.show]:
        passed = sum(1 for entry in ledger if entry["event"] == "checked" and entry.get("kept")
                     and entry.get("machine") == offer["machine_id"])
        print(json.dumps({"offer": offer["id"], "price": round(offer["dph_total"], 3), "where": offer.get("geolocation"),
                          "host": offer["host_id"], "machine": offer["machine_id"],
                          "power_limit_w": offer.get("gpu_max_power"), "reliability": offer.get("reliability2"),
                          "verification": offer.get("verification"), "passed_checks": passed,
                          "host_failures": failures[(PROVIDER, offer["host_id"])]}, ensure_ascii=False))
    return 0


def rent(args):
    offers = [offer for offer in choose(search(args.gpu, args.count, args.disk, args.query), entries())
              if matches(offer, args.max_price, args.countries)]
    public_key, failed = config()["ssh_public_key"], 0
    for offer in offers:
        if failed == args.tries:
            break
        outcome, rented = rent_one(offer, args, public_key)
        if outcome == "kept":
            instance, results = rented
            print(json.dumps({"rented": instance, "price": round(offer["dph_total"], 3),
                              "where": offer.get("geolocation"), "machine": offer["machine_id"],
                              "host": offer["host_id"], "check": summary(results)}, ensure_ascii=False))
            return 0
        failed += outcome == "failed"
    print(json.dumps({"rented": None, "offers": len(offers), "failed": failed}))
    return 1


def run(args):
    words = args.command[1:] if args.command[:1] == ["--"] else args.command
    base = ssh_command(args.instance)
    if base is None:
        print("no route accepted ssh", file=sys.stderr)
        return 255
    try:
        done = subprocess.run([*base, " ".join(words)], stderr=subprocess.PIPE, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print(f"the command ran longer than {args.timeout} seconds", file=sys.stderr)
        return 124
    sys.stderr.write(hide_addresses(done.stderr.decode(errors="replace")))
    return done.returncode


def held_or_exit(instance):
    held = rental(entries(), instance)
    if held is None:
        raise SystemExit(f"the ledger has no rental {instance}")
    return held


def extend(args):
    if held_or_exit(args.instance)["returned"]:
        raise SystemExit(f"{args.instance} went back already")
    entry = record("extended", provider=PROVIDER, instance=args.instance,
                   deadline=stamp(now() + timedelta(hours=args.hours)))
    print(json.dumps({"instance": args.instance, "deadline": entry["deadline"]}))
    return 0


def note(args):
    held = held_or_exit(args.instance)
    record("noted", provider=PROVIDER, instance=args.instance, machine=held["machine"], host=held["host"],
           gpu=held["gpu"], note=args.text, values=args.values or {})
    return 0


def give_back(args):
    held = held_or_exit(args.instance)
    if held["returned"]:
        print(json.dumps({"instance": args.instance, "returned": "before"}))
        return 0
    entry = finish(args.instance, by="command")
    print(json.dumps({"returned": args.instance, "hours": entry["hours"], "dollars": entry["dollars"]}))
    return 0


def show_ledger(args):
    ledger, machines = entries(), {}
    for entry in ledger:
        if entry.get("machine") is None or (args.gpu and entry.get("gpu") != args.gpu):
            continue
        row = machines.setdefault((entry["provider"], entry["machine"]), {
            "machine": entry["machine"], "host": entry.get("host"), "gpu": entry.get("gpu"), "where": entry.get("where"),
            "rented": 0, "passed": 0, "failed": [], "notes": []})
        if entry["event"] == "rented":
            row["rented"] += 1
            row["last"] = entry["at"][:10]
        elif entry["event"] == "checked" and entry.get("kept"):
            row["passed"] += 1
            row["check"] = summary(entry["results"])
        elif entry["event"] == "unusable":
            row["failed"].append(f"{entry['stage']}: {entry.get('detail', '')[:120]}")
        elif entry["event"] == "noted":
            row["notes"].append({"note": entry["note"], **entry.get("values", {})})
    for gpu in sorted({row["gpu"] for row in machines.values() if row["gpu"]}):
        medians, count = typical(ledger, gpu, machine_check.VERSION)
        print(json.dumps({"gpu": gpu, "passed_machines": count,
                          "medians": {name: round(value, 1) for name, value in medians.items()}}))
    for row in machines.values():
        print(json.dumps(row, ensure_ascii=False))
    return 0


def parser():
    found = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # Foundation passes on no environment variable but the key, so these reach a command run under it as options.
    found.add_argument("--home", help=argparse.SUPPRESS)
    found.add_argument("--config", help=argparse.SUPPRESS)
    found.add_argument("--under-foundation", action="store_true", help=argparse.SUPPRESS)
    commands = found.add_subparsers(dest="command", required=True)

    def searching(command):
        command.add_argument("gpu", help='the GPU as Vast.ai names it, such as "RTX 5090" or "H100 SXM"')
        command.add_argument("--count", type=int, default=1, help="GPUs a machine")
        command.add_argument("--max-price", type=float, help="dollars an hour, with the disk")
        command.add_argument("--countries", type=lambda text: [code for code in text.split(",") if code],
                             help="country codes, comma-separated")
        command.add_argument("--disk", type=float, default=60, help="GB")
        command.add_argument("--query", type=json.loads, help="more of Vast.ai's search filters, as JSON")

    offers = commands.add_parser("offers", help="show the offers that rent would try, in its order")
    searching(offers)
    offers.add_argument("--show", type=int, default=10)
    renting = commands.add_parser("rent", help="rent and check machines until one passes")
    searching(renting)
    renting.add_argument("--image", required=True)
    renting.add_argument("--label", required=True)
    renting.add_argument("--hours", type=float, required=True, help="until the guard returns the machine")
    renting.add_argument("--tries", type=int, default=3, help="machines that may fail before rent gives up")
    renting.add_argument("--min-download", type=float, default=0, help="MB/s that the work needs")
    renting.add_argument("--env", type=json.loads, help="the container's environment, as JSON")
    renting.add_argument("--onstart", help="a command the container runs when it starts")
    running = commands.add_parser("run", help="run a command on the machine over SSH")
    running.add_argument("instance", type=int)
    running.add_argument("--timeout", type=int, default=600)
    running.add_argument("command", nargs=argparse.REMAINDER)
    extending = commands.add_parser("extend", help="move the guard's deadline to this many hours from now")
    extending.add_argument("instance", type=int)
    extending.add_argument("hours", type=float)
    noting = commands.add_parser("note", help="record how the work ran on the machine")
    noting.add_argument("instance", type=int)
    noting.add_argument("text")
    noting.add_argument("--values", type=json.loads, help="measurements, as a JSON object")
    returning = commands.add_parser("return", help="destroy the machine")
    returning.add_argument("instance", type=int)
    listing = commands.add_parser("ledger", help="show how every machine did, and each GPU's medians")
    listing.add_argument("gpu", nargs="?")
    guarding = commands.add_parser("guard", help=argparse.SUPPRESS)
    guarding.add_argument("instance", type=int)
    return found


COMMANDS = {"offers": list_offers, "rent": rent, "run": run, "extend": extend, "note": note, "return": give_back,
            "ledger": show_ledger, "guard": lambda args: guard(args.instance)}


def foundation_seconds(args):
    """How long Foundation lets a command run."""
    if args.command == "rent":
        return args.tries * (START_SECONDS + SSH_SECONDS + CHECK_SECONDS + 120) + 600
    if args.command == "run":
        return args.timeout + 120
    if args.command == "guard":
        return GUARD_LIFETIME_SECONDS
    return 600


def under_foundation(argv, seconds, settings):
    """The command that runs this script again under Foundation with the Vast.ai key. Foundation passes on no other
    environment variable, so the ledger's folder and the settings go on the command line."""
    inputs = [{"name": "VAST_API_KEY", "source": settings["vast_api_key"]}]
    return ["foundation", "exec", "--inputs", json.dumps(inputs), "--timeout", str(seconds), "--",
            sys.executable, str(Path(__file__).resolve()), "--under-foundation", "--home", str(home()),
            "--config", str(config_path()), *argv]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(argv)
    if args.home:
        os.environ["GPU_RENTAL_HOME"] = args.home
    if args.config:
        os.environ["GPU_RENTAL_CONFIG"] = args.config
    if args.command in KEYED and not args.under_foundation:
        return subprocess.run(under_foundation(argv, foundation_seconds(args), config())).returncode
    if args.command in KEYED and not os.environ.get("VAST_API_KEY"):
        # Never run itself under Foundation a second time: a command that went without the key would do so forever.
        raise SystemExit("Foundation did not deliver VAST_API_KEY")
    try:
        return COMMANDS[args.command](args)
    except ImageFault as fault:
        print(f"the image did not start, and the machine went back: {fault}", file=sys.stderr)
        return 2
    except RuntimeError as error:
        print(f"{error}, and the machine went back", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
