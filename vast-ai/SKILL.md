---
name: vast-ai
description: Rent a GPU machine on Vast.ai, run work on it, and delete it. Use whenever a task rents or manages Vast.ai instances.
---

# vast-ai

- Before renting, give the user the machine, the expected hours and the
  total cost, and wait for approval. If the project already has a script
  that rents for this kind of work, use it instead of writing a new one.
- Rent only Ampere or newer GPUs, on hosts outside China and France, whose
  driver supports the CUDA version of the image.
- Before starting the work, check that the GPUs hold almost no memory: a
  host whose GPU held another process's memory ran training out of memory.
- Decide that the work ended only from an exit-code file it writes when it
  ends, never from its log: a log line that started with a digit was once
  taken for an exit code.
- Delete the instance whether the work succeeded or failed, and then check
  that nothing with its label is left.
- Never print IP addresses, SSH hosts or ports, keys or presigned URLs.
