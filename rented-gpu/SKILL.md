---
name: rented-gpu
description: Rules for a GPU machine rented by the hour, whichever provider, from choosing it to returning it, so that its paid time goes to the work itself. Use whenever a task rents a GPU or runs work on a rented GPU.
---

# rented-gpu

A rented GPU is paid by the hour, is usually far away, and needs its work and
data again every time it is rented. Keep its paid time for the work itself.
How to rent from a provider is in that provider's skill, such as vast-ai.

- Between here and the rented machine, send only commands and checks, over
  ssh. Large things, such as data and weights, go between the rented machine
  and object storage directly, not through here: one stream to a far machine
  is slow and can stall. From Singapore in November 2026, one ssh stream to
  Europe ran at about 8 MB/s, while machines in Europe and the US downloaded
  from R2 at about 90 MB/s. This environment's object storage is Cloudflare
  R2; the cloudflare-r2 skill says how to use it.
- Rent only machines whose driver supports the image's CUDA version. Before
  the work, check that the GPU holds almost no memory. If the work runs much
  slower than on other machines with the same GPU, change machines.
- Rent with a deadline that returns the machine even if the session ends, and
  return it as soon as the work ends.
