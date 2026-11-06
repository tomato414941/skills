"""Measure a rented machine in about a minute and print one JSON object: what nvidia-smi reports of each GPU, how fast
each GPU multiplies bfloat16 matrices and copies within its memory, its SM clock and power draw while it multiplies,
how fast the host copies to the first GPU, and how fast the machine downloads a public file and writes to its disk.

It runs with the Python of the machine's image, which needs PyTorch for the GPUs' speeds: python3 - < machine_check.py
A change to what or how it measures changes VERSION, since only checks of one version are compared.
"""

import json
import os
import statistics
import subprocess
import tempfile
import time

VERSION = 1
QUERY = ("name", "memory.used", "memory.total", "power.limit", "power.default_limit", "power.max_limit",
         "clocks.max.sm", "pcie.link.gen.max", "pcie.link.width.max", "temperature.gpu", "driver_version")
KEYS = ("name", "memory_used_mib", "memory_total_mib", "power_limit_w", "power_default_w", "power_max_w",
        "sm_clock_max_mhz", "pcie_gen", "pcie_width", "temperature_c", "driver")
TEXT = {"name", "driver"}
MATRIX = 8192
# A public file of about 2 GB; the first 512 MiB of it are timed, so that hosts that charge for traffic charge little.
DOWNLOAD = "https://huggingface.co/datasets/HuggingFaceFW/fineweb-edu/resolve/main/sample/10BT/000_00000.parquet"
DOWNLOAD_BYTES = 512 * 2**20
DOWNLOAD_SECONDS = 15


def number(text):
    try:
        return float(text)
    except ValueError:
        return None


def parse_gpus(text):
    """nvidia-smi's rows, without header or units, as one dictionary a GPU; a value it lacks, like "[N/A]", is None."""
    rows = []
    for line in text.splitlines():
        if line.strip():
            values = [value.strip() for value in line.split(",")]
            rows.append({key: value if key in TEXT else number(value) for key, value in zip(KEYS, values)})
    return rows


def gpus():
    done = subprocess.run(["nvidia-smi", "--query-gpu=" + ",".join(QUERY), "--format=csv,noheader,nounits"],
                          capture_output=True, text=True, timeout=60)
    return parse_gpus(done.stdout)


def parse_samples(text):
    """The medians of the SM clock and of the power draw in nvidia-smi's samples of the two."""
    rows = [[number(value.strip()) for value in line.split(",")] for line in text.splitlines() if line.count(",") == 1]
    clocks = [clock for clock, _ in rows if clock]
    draws = [draw for _, draw in rows if draw]
    return (statistics.median(clocks) if clocks else None, statistics.median(draws) if draws else None)


def rate(step, seconds, synchronize):
    """How many times a second `step` runs, timed over at least `seconds` after one untimed run."""
    step()
    synchronize()
    count, began = 0, time.perf_counter()
    while True:
        for _ in range(5):
            step()
        count += 5
        synchronize()
        elapsed = time.perf_counter() - began
        if elapsed >= seconds:
            return count / elapsed


def gpu_speeds(count):
    """Each GPU's speeds, and the slowest GPU's as the machine's: training on several GPUs waits for the slowest."""
    import torch

    found = {"torch": torch.__version__, "cuda": torch.version.cuda, "each": []}
    for index in range(count):
        device = torch.device("cuda", index)

        def synchronize():
            torch.cuda.synchronize(device)

        left, right = (torch.randn(MATRIX, MATRIX, device=device, dtype=torch.bfloat16) for _ in range(2))
        product = torch.empty_like(left)
        sampler = subprocess.Popen(["nvidia-smi", "-i", str(index), "--query-gpu=clocks.sm,power.draw",
                                    "--format=csv,noheader,nounits", "-lms", "500"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            products = rate(lambda: torch.mm(left, right, out=product), 6, synchronize)
        finally:
            sampler.terminate()
            samples, _ = sampler.communicate(timeout=30)
        clock, draw = parse_samples(samples)
        source = torch.empty(2**28, device=device)  # 1 GiB of float32
        target = torch.empty_like(source)
        copies = rate(lambda: target.copy_(source), 2, synchronize)
        found["each"].append({"matmul_tflops": products * 2 * MATRIX**3 / 1e12,
                              "memory_gbps": copies * 2 * source.numel() * source.element_size() / 1e9,
                              "sm_clock_under_load_mhz": clock, "power_under_load_w": draw})
        del left, right, product, source, target
        torch.cuda.empty_cache()
    host = torch.empty(2**28, pin_memory=True)
    on_gpu = torch.empty(2**28, device="cuda:0")
    uploads = rate(lambda: on_gpu.copy_(host, non_blocking=True), 2, lambda: torch.cuda.synchronize(0))
    found["host_to_gpu_gbps"] = uploads * host.numel() * host.element_size() / 1e9
    for name in ("matmul_tflops", "memory_gbps", "sm_clock_under_load_mhz", "power_under_load_w"):
        values = [each[name] for each in found["each"] if each[name] is not None]
        found[name] = min(values) if values else None
    return found


def download_mbps():
    try:
        done = subprocess.run(["curl", "-sS", "-L", "-o", "/dev/null", "-r", f"0-{DOWNLOAD_BYTES - 1}",
                               "--max-time", str(DOWNLOAD_SECONDS), "-w", "%{http_code} %{size_download} %{time_total}",
                               DOWNLOAD], capture_output=True, text=True, timeout=DOWNLOAD_SECONDS + 30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    words = done.stdout.split()
    if len(words) != 3 or not words[0].startswith("2") or float(words[2]) <= 0:
        return None
    return float(words[1]) / float(words[2]) / 1e6


def disk_write_mbps(directory, size=2**30):
    block = os.urandom(2**20) * 64
    with tempfile.NamedTemporaryFile(dir=directory) as stream:
        began = time.perf_counter()
        for _ in range(size // len(block)):
            stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())
        return size / (time.perf_counter() - began) / 1e6


def host():
    found = {"cpus": len(os.sched_getaffinity(0))}
    try:
        with open("/proc/cpuinfo") as lines:
            found["cpu"] = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("model name")), None)
        with open("/proc/meminfo") as lines:
            found["memory_gib"] = next((round(int(line.split()[1]) / 2**20, 1) for line in lines
                                        if line.startswith("MemTotal")), None)
    except OSError:
        pass
    return found


def main():
    began = time.time()
    results = {"version": VERSION, "gpus": gpus(), **host()}
    try:
        results.update(gpu_speeds(len(results["gpus"])))
    except Exception as error:  # noqa: BLE001 - a machine that cannot measure its GPUs still reports the rest
        results["speed_error"] = f"{type(error).__name__}: {error}"[-300:]
    results["download_mbps"] = download_mbps()
    try:
        results["disk_write_mbps"] = disk_write_mbps(".")
    except OSError as error:
        results["disk_error"] = str(error)[-300:]
    results["seconds"] = round(time.time() - began, 1)
    print(json.dumps(results))


if __name__ == "__main__":
    main()
