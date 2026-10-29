---
name: vast
description: Rent a GPU machine on Vast.ai, run work on it, and delete it. Use whenever a task rents or manages Vast.ai instances.
---

# vast

Use the `vastai` CLI (`uv run --no-project --with vastai==1.8.3 vastai ...`).
It reads the key from `VAST_API_KEY`; deliver it with the secret store's exec
command placed before `uv run`.

## Before renting

- Renting costs money: give the user the machine, the expected hours and the
  total before creating an instance, and wait for approval.
- If the project already has a script that rents for this kind of work, use
  it instead of writing a new one.

## Choosing an offer

```sh
vastai search offers 'compute_cap>=800 cuda_vers>=<image CUDA> num_gpus=1 gpu_ram>=<GB> reliability>0.98 verified=true geolocation notin [CN,FR]' -o 'dph_total'
```

- `compute_cap>=800`: Ampere or newer. Older GPUs such as the V100 cannot run
  PyTorch's CUDA 13 build.
- `cuda_vers` at least the CUDA of the image: a CUDA 13 image does not start
  on a host whose driver supports only 12.x.
- No hosts in China or France.

## Running

- Create with `--ssh --direct --cancel-unavail`, a `--label` that names the
  work, and enough `--disk`.
- After the instance runs, check `nvidia-smi` shows its GPUs nearly empty
  before starting; a host with another process on the GPU runs out of memory.
- Run long work in the background and have it write an exit-code file when
  it ends, such as `echo FINISHED=$? > DONE`. Decide that the work ended only
  from that file; a log line that happens to start with a digit is not an
  exit code.
- Do not print IP addresses, SSH hosts or ports, keys or presigned URLs.
  `vastai ssh-url` prints an address: use it only inside a command.

## Ending

- Destroy the instance (`vastai destroy instance <id>`) whether the work
  succeeded or failed, in a `finally` block or a trap.
- Then check `vastai show instances` lists nothing left with the label.
