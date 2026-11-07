---
name: rented-gpu
description: Rules for a GPU machine rented by the hour, whichever provider, from choosing it to returning it, so that its paid time goes to the work itself. Use whenever a task rents a GPU or runs work on a rented GPU.
---

# rented-gpu

A rented GPU is paid by the hour, is usually far away, and needs its work and
data again every time it is rented. Keep its paid time for the work itself.

- Between the machine you work from and the rented machine, send only
  commands and checks, over ssh. Large things, such as data and weights, go
  between the rented machine and object storage directly: one stream to a
  far machine is slow and can stall. In November 2026, one ssh stream to a
  machine 200 ms away ran at about 8 MB/s, while that machine downloaded
  from object storage at about 90 MB/s with parallel byte ranges.
- Give the rented machine a presigned URL for each object it fetches or
  stores, never a storage key, and pass the URL on standard input, so that
  it shows in no command line or log.
- Rent only machines whose driver supports the image's CUDA version. Before
  the work, check that the GPU holds almost no memory. If the work runs much
  slower than on other machines with the same GPU, change machines.
- Rent with a deadline that returns the machine even if the session ends, and
  return it as soon as the work ends.
