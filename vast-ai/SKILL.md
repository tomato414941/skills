---
name: vast-ai
description: Rent a GPU machine on Vast.ai, run work on it, and delete it. Use whenever a task rents or manages Vast.ai instances.
---

# vast-ai

- Rent only hosts whose driver supports the CUDA version of the image.
- Some hosts run at half the usual speed. If the work runs much slower than
  expected, stop using that host and rent another.
- Before starting the work, check that the GPUs hold almost no memory: a
  host whose GPU held another process's memory ran training out of memory.
