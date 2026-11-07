---
name: cloudflare-r2
description: Keep data in Cloudflare R2 and move large data between machines through it. Use whenever a task stores data in R2, makes a bucket or its key, or sends large data to another machine, such as a rented GPU.
---

# cloudflare-r2

- Send large data to a rented machine through R2, not over ssh from here:
  upload it to the project's bucket from the machine that holds it, and have
  the rented machine download it itself. Measured from Singapore in November
  2026, one ssh stream to Europe ran at about 8 MB/s and another stalled,
  while machines in Europe and the US downloaded from R2 at about 90 MB/s
  with parallel byte ranges.
- Give each project its own bucket, and a key that reaches only that
  bucket's objects. Keep the key in the project's `.env`. Do not use another
  project's bucket.
- Give other machines a presigned URL for each request instead of the key,
  and pass it on standard input, so that it shows in no command line or log.
- Make a bucket and its key with the Cloudflare API token kept in Foundation.
