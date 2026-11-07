---
name: ovh
description: Rent servers, VPS and GPUs on OVHcloud, and wait for VPS stock. Read this when renting from OVHcloud or when choosing where to run work.
---

# ovh

- A stopped Public Cloud instance costs the same as a running one. On hourly
  billing, a shelved one costs only its snapshot.
- Monthly billing cannot go back to hourly.
- A manual quota increase is charged at once.
- A VPS place can show `status: available` while only Windows images are in
  stock. Check `linuxStatus` in `/vps/order/rule/datacenter`.
- A 2027 VPS plan needs its local storage and automatic backup options in the
  cart before the checkout.
- An API consumer key can be limited to the addresses of the machine it was
  made on; from another machine its calls are refused.
- To wait for a VPS that is out of stock, add it to `scripts/vps_watch.py` in
  this folder:
  `python3 scripts/vps_watch.py add NAME PLAN MAX_PRICE --places any`.
  It orders each target once, in the first allowed place with Linux stock,
  and stops a target whose price is above its cap. `list` shows the targets
  and `check` shows where each could be ordered now. `run` orders
  whatever is in stock; have a timer run it every few minutes.
