"""Checks of gpu_rental.py and machine_check.py without Vast.ai, SSH, Foundation or a GPU.

Run in this folder: python3 -m unittest
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import gpu_rental
import machine_check


def offer(machine, host, price, gpu="RTX 5090", power=575.0):
    return {"id": machine * 10, "machine_id": machine, "host_id": host, "dph_total": price, "gpu_name": gpu,
            "num_gpus": 1, "gpu_max_power": power, "geolocation": "Taiwan, TW", "reliability2": 0.99,
            "verification": "verified"}


def results(matmul=100.0, memory=1500.0, used=0.0, limit=575.0, default=575.0, download=500.0,
            version=machine_check.VERSION):
    return {"version": version, "matmul_tflops": matmul, "memory_gbps": memory, "download_mbps": download,
            "gpus": [{"name": "NVIDIA GeForce RTX 5090", "memory_used_mib": used, "power_limit_w": limit,
                      "power_default_w": default}]}


def renting_arguments(**changes):
    return SimpleNamespace(**{"gpu": "RTX 5090", "count": 1, "disk": 60, "query": None, "max_price": None,
                              "countries": None, "image": "image", "label": "test", "hours": 1.0, "tries": 3,
                              "min_download": 0, "env": None, "onstart": None, **changes})


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        variables = patch.dict(os.environ, {"GPU_RENTAL_HOME": directory.name})
        variables.start()
        self.addCleanup(variables.stop)

    def passed(self, machine, gpu="RTX 5090", **measured):
        gpu_rental.record("checked", provider="vast", instance=machine, machine=machine, host=machine, gpu=gpu,
                          results=results(**measured), kept=True, reasons=[])

    def failed(self, machine, host, stage="start"):
        gpu_rental.record("unusable", provider="vast", instance=machine, machine=machine, host=host, gpu="RTX 5090",
                          stage=stage, detail="")

    def rented(self, instance, deadline, price=0.5):
        gpu_rental.record("rented", provider="vast", instance=instance, machine=instance, host=instance,
                          gpu="RTX 5090", price=price, deadline=gpu_rental.stamp(deadline))

    def events(self):
        return [(entry["event"], entry.get("instance")) for entry in gpu_rental.entries()]


class LedgerTest(LedgerTestCase):
    def test_reads_back_the_entries_in_the_order_they_were_recorded(self):
        gpu_rental.record("first", value=1)
        gpu_rental.record("second", value=2)
        self.assertEqual([(entry["event"], entry["value"]) for entry in gpu_rental.entries()],
                         [("first", 1), ("second", 2)])

    def test_skips_a_line_that_a_writer_left_unfinished(self):
        gpu_rental.record("whole")
        with open(gpu_rental.home() / "ledger.jsonl", "a") as stream:
            stream.write('{"at": "2026-11-06T00:00:00+00:00", "ev')
        self.assertEqual([entry["event"] for entry in gpu_rental.entries()], ["whole"])

    def test_follows_a_rentals_latest_deadline_until_it_goes_back(self):
        moment = gpu_rental.now()
        self.rented(7, moment)
        gpu_rental.record("extended", provider="vast", instance=7, deadline=gpu_rental.stamp(moment + timedelta(hours=2)))
        held = gpu_rental.rental(gpu_rental.entries(), 7)
        self.assertEqual(held["deadline"], gpu_rental.stamp(moment + timedelta(hours=2)))
        self.assertFalse(held["returned"])
        gpu_rental.record("returned", provider="vast", instance=7, by="command")
        self.assertTrue(gpu_rental.rental(gpu_rental.entries(), 7)["returned"])


class ChoiceTest(LedgerTestCase):
    def test_leaves_out_machines_that_failed_once(self):
        self.failed(1, host=10)
        chosen = gpu_rental.choose([offer(1, 10, 0.4), offer(2, 20, 0.5)], gpu_rental.entries())
        self.assertEqual([each["machine_id"] for each in chosen], [2])

    def test_puts_hosts_that_failed_before_after_the_others_each_group_cheapest_first(self):
        self.failed(1, host=10)
        offers = [offer(3, 10, 0.4), offer(4, 20, 0.7), offer(5, 30, 0.6), offer(6, 10, 0.3)]
        chosen = gpu_rental.choose(offers, gpu_rental.entries())
        self.assertEqual([each["machine_id"] for each in chosen], [5, 4, 6, 3])

    def test_leaves_out_offers_whose_power_limit_is_below_the_default_of_checked_machines_with_the_same_gpu(self):
        self.passed(1, default=575.0)
        offers = [offer(2, 20, 0.4, power=450.0), offer(3, 30, 0.5, power=575.0), offer(4, 40, 0.6, power=600.0),
                  offer(5, 50, 0.3, gpu="RTX 4090", power=450.0)]
        chosen = gpu_rental.choose(offers, gpu_rental.entries())
        self.assertEqual([each["machine_id"] for each in chosen], [5, 3, 4])

    def test_keeps_offers_of_a_gpu_whose_default_power_no_check_has_reported(self):
        chosen = gpu_rental.choose([offer(2, 20, 0.4, power=450.0)], gpu_rental.entries())
        self.assertEqual([each["machine_id"] for each in chosen], [2])


class VerdictTest(LedgerTestCase):
    def test_keeps_a_machine_that_matches_its_gpus_checked_machines(self):
        self.passed(1)
        self.passed(2)
        self.assertEqual(gpu_rental.verdict(results(matmul=95.0), gpu_rental.entries(), "RTX 5090"), [])

    def test_sends_back_a_machine_whose_gpu_holds_memory(self):
        reasons = gpu_rental.verdict(results(used=14000.0), gpu_rental.entries(), "RTX 5090")
        self.assertEqual([reason.split(":")[0] for reason in reasons], ["gpu_busy"])

    def test_sends_back_a_machine_whose_power_is_capped_below_its_default(self):
        reasons = gpu_rental.verdict(results(limit=450.0), gpu_rental.entries(), "RTX 5090")
        self.assertEqual([reason.split(":")[0] for reason in reasons], ["power_capped"])

    def test_sends_back_a_machine_slower_than_the_median_of_checked_machines_with_its_gpu(self):
        self.passed(1, matmul=100.0)
        self.passed(2, matmul=104.0)
        reasons = gpu_rental.verdict(results(matmul=80.0), gpu_rental.entries(), "RTX 5090")
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith("slow: matmul_tflops 80 against a median of 102 over 2 machines"))

    def test_compares_a_machine_only_with_checked_machines_with_its_gpu_and_its_checks_version(self):
        self.passed(1, gpu="H100 SXM", matmul=600.0)
        self.passed(2, gpu="H100 SXM", matmul=600.0)
        self.passed(3, matmul=100.0)
        self.passed(4, matmul=100.0, version=machine_check.VERSION + 1)
        self.assertEqual(gpu_rental.verdict(results(matmul=50.0), gpu_rental.entries(), "RTX 5090"), [])

    def test_sends_back_a_machine_whose_download_is_slower_than_the_work_needs(self):
        reasons = gpu_rental.verdict(results(download=40.0), gpu_rental.entries(), "RTX 5090", min_download_mbps=100)
        self.assertEqual([reason.split(":")[0] for reason in reasons], ["slow_download"])


class ReturnTest(LedgerTestCase):
    def test_returns_the_machine_at_its_deadline(self):
        self.rented(7, gpu_rental.now() - timedelta(minutes=1))
        destroyed = []
        with patch.object(gpu_rental, "destroy", destroyed.append):
            self.assertEqual(gpu_rental.guard(7, poll=0), 0)
        self.assertEqual(destroyed, [7])
        self.assertEqual(gpu_rental.entries()[-1]["by"], "guard")

    def test_stops_guarding_a_machine_that_went_back(self):
        self.rented(7, gpu_rental.now() - timedelta(minutes=1))
        gpu_rental.record("returned", provider="vast", instance=7, by="command")
        with patch.object(gpu_rental, "destroy", side_effect=AssertionError("destroyed twice")):
            self.assertEqual(gpu_rental.guard(7, poll=0), 0)

    def test_records_no_return_for_a_machine_that_could_not_be_destroyed(self):
        self.rented(7, gpu_rental.now())
        with patch.object(gpu_rental, "destroy", side_effect=gpu_rental.VastError(500, "busy")), \
                patch.object(gpu_rental, "show", lambda instance: {"id": instance}):
            with self.assertRaises(gpu_rental.VastError):
                gpu_rental.finish(7, by="command")
        self.assertFalse(gpu_rental.rental(gpu_rental.entries(), 7)["returned"])

    def test_records_no_return_when_vast_answers_that_the_destruction_failed_and_the_machine_is_still_there(self):
        self.rented(7, gpu_rental.now())
        with patch.object(gpu_rental, "destroy", lambda instance: {"success": False, "msg": "try later"}), \
                patch.object(gpu_rental, "show", lambda instance: {"id": instance}):
            with self.assertRaises(gpu_rental.VastError):
                gpu_rental.finish(7, by="command")
        self.assertFalse(gpu_rental.rental(gpu_rental.entries(), 7)["returned"])

    def test_records_the_return_of_a_machine_that_is_gone_already(self):
        self.rented(7, gpu_rental.now(), price=0.6)
        with patch.object(gpu_rental, "destroy", side_effect=gpu_rental.VastError(404, "gone")), \
                patch.object(gpu_rental, "show", lambda instance: None):
            entry = gpu_rental.finish(7, by="command")
        self.assertEqual((entry["event"], entry["by"]), ("returned", "command"))
        self.assertTrue(gpu_rental.rental(gpu_rental.entries(), 7)["returned"])


class RentTest(LedgerTestCase):
    def renting(self, offers, measured, taken=()):
        """Fakes Vast.ai, the guard and SSH: someone else rents the offers of the machines in `taken` first, and the
        machine of any other offer reports measured[machine], or does not run the check if that is None."""
        self.destroyed = []

        def create(chosen, *rest):
            if chosen["machine_id"] in taken:
                raise gpu_rental.VastError(400, "the offer is no longer available")
            return 1000 + chosen["machine_id"]

        def check(instance):
            found = measured[instance - 1000]
            return (found, None) if found is not None else (None, "the check failed with 127: python3: not found")

        stack = contextlib.ExitStack()
        for name, fake in {"search": lambda *arguments: offers, "create": create, "start_guard": lambda instance: True,
                           "wait_until_running": lambda instance: None, "attach": lambda instance, key: {},
                           "wait_for_ssh": lambda instance: instance, "check": check,
                           "destroy": self.destroyed.append, "show": lambda instance: None,
                           "config": lambda: {"ssh_public_key": "ssh-ed25519 AAAA test"}}.items():
            stack.enter_context(patch.object(gpu_rental, name, fake))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return stack

    def test_sends_back_a_machine_that_fails_its_check_and_keeps_the_next_one_that_passes(self):
        with self.renting([offer(1, 10, 0.5), offer(2, 20, 0.6)], {1: results(limit=450.0), 2: results()}):
            self.assertEqual(gpu_rental.rent(renting_arguments()), 0)
        self.assertEqual(self.destroyed, [1001])
        self.assertEqual(self.events(), [("rented", 1001), ("checked", 1001), ("unusable", 1001), ("returned", 1001),
                                         ("rented", 1002), ("checked", 1002)])
        self.assertEqual(gpu_rental.avoided(gpu_rental.entries()), {("vast", 1)})

    def test_gives_up_after_as_many_failed_machines_as_it_may_try(self):
        offers = [offer(machine, machine * 10, 0.5) for machine in (1, 2, 3)]
        with self.renting(offers, {machine: results(used=9000.0) for machine in (1, 2, 3)}):
            self.assertEqual(gpu_rental.rent(renting_arguments(tries=2)), 1)
        self.assertEqual(self.destroyed, [1001, 1002])

    def test_moves_on_from_an_offer_that_someone_rented_first(self):
        with self.renting([offer(1, 10, 0.5), offer(2, 20, 0.6)], {2: results()}, taken={1}):
            self.assertEqual(gpu_rental.rent(renting_arguments(tries=1)), 0)
        self.assertEqual(self.events(), [("rented", 1002), ("checked", 1002)])

    def test_sends_back_the_machine_without_blaming_it_when_the_check_does_not_run(self):
        with self.renting([offer(1, 10, 0.5)], {1: None}):
            with self.assertRaises(RuntimeError):
                gpu_rental.rent(renting_arguments())
        self.assertEqual(self.destroyed, [1001])
        self.assertEqual(self.events(), [("rented", 1001), ("returned", 1001)])
        self.assertEqual(gpu_rental.avoided(gpu_rental.entries()), set())


class FoundationTest(LedgerTestCase):
    def test_runs_itself_again_under_foundation_with_the_key_and_the_ledgers_folder_on_the_command_line(self):
        settings = {"vast_api_key": {"id": "item", "output": "VAST_API_KEY"}}
        command = gpu_rental.under_foundation(["return", "7"], 100, settings)
        self.assertEqual(command[:2], ["foundation", "exec"])
        self.assertEqual(json.loads(command[3]), [{"name": "VAST_API_KEY", "source": settings["vast_api_key"]}])
        self.assertEqual(command[command.index("--home") + 1], str(gpu_rental.home()))
        self.assertIn("--under-foundation", command)
        self.assertEqual(command[-2:], ["return", "7"])

    def test_never_runs_itself_under_foundation_a_second_time(self):
        os.environ.pop("VAST_API_KEY", None)
        with patch.object(gpu_rental.subprocess, "run", side_effect=AssertionError("ran itself again")):
            with self.assertRaises(SystemExit):
                gpu_rental.main(["--under-foundation", "return", "7"])

    def test_reaches_a_machine_through_an_agent_that_foundation_fills_with_the_tools_key(self):
        with patch.object(gpu_rental, "config", lambda: {"ssh_key": "key"}):
            command = gpu_rental.ssh_base(7, "203.0.113.9", 41022)
        self.assertEqual(command[:6], ["foundation", "ssh-key", "agent", "key", "--", "ssh"])
        self.assertEqual(command[-1], "root@203.0.113.9")


class AddressTest(unittest.TestCase):
    def test_hides_addresses_urls_and_vast_host_names_but_not_times(self):
        text = "00:03:38 ssh: connect to host 203.0.113.9 port 4022 via ssh5.vast.ai, see https://example.com/x?a=1"
        self.assertEqual(gpu_rental.hide_addresses(text),
                         "00:03:38 ssh: connect to host <ip> port 4022 via <host>, see <url>")

    def test_tries_the_hosts_own_port_before_vasts_proxy(self):
        row = {"public_ipaddr": "203.0.113.9", "ports": {"22/tcp": [{"HostPort": "41022"}]},
               "ssh_host": "ssh5.vast.ai", "ssh_port": 12345}
        self.assertEqual(gpu_rental.routes(row), [("203.0.113.9", "41022"), ("ssh5.vast.ai", 12345)])
        self.assertEqual(gpu_rental.routes({"ssh_host": "ssh5.vast.ai", "ssh_port": 12345}), [("ssh5.vast.ai", 12345)])


class MachineCheckTest(unittest.TestCase):
    def test_reads_nvidia_smis_rows_with_missing_values_as_none(self):
        rows = machine_check.parse_gpus("NVIDIA GeForce RTX 5090, 3, 32607, 450.00, 575.00, 600.00, 3090, [N/A], 16, "
                                        "35, 580.95.05\n")
        self.assertEqual(rows, [{"name": "NVIDIA GeForce RTX 5090", "memory_used_mib": 3.0, "memory_total_mib": 32607.0,
                                 "power_limit_w": 450.0, "power_default_w": 575.0, "power_max_w": 600.0,
                                 "sm_clock_max_mhz": 3090.0, "pcie_gen": None, "pcie_width": 16.0,
                                 "temperature_c": 35.0, "driver": "580.95.05"}])

    def test_takes_the_medians_of_the_sampled_clock_and_power(self):
        self.assertEqual(machine_check.parse_samples("2160, 449.12\n2145, 450.01\n2175, 448.90\n"), (2160.0, 449.12))


if __name__ == "__main__":
    unittest.main()
