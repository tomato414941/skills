---
name: cloudflare-r2
description: Keep data in Cloudflare R2, this environment's object storage. Use whenever a task stores data in R2 or makes a bucket or its key.
---

# cloudflare-r2

- Give each project its own bucket, and a key that reaches only that
  bucket's objects. Keep the key in the project's `.env`. Do not use another
  project's bucket.
- Give other machines a presigned URL for each request instead of the key,
  and pass it on standard input, so that it shows in no command line or log.
- Make a bucket and its key with the Cloudflare API token kept in Foundation.
