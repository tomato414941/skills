---
name: cloudflare-r2
description: Keep data in Cloudflare R2 through its S3 API. Use whenever a task stores data in R2 or makes an R2 bucket or its key.
---

# cloudflare-r2

- Give each project its own bucket, and a key that reaches only that
  bucket's objects. Keep the key in the project's `.env`. Do not use another
  project's bucket.
- Give other machines a presigned URL for each request instead of the key,
  and pass it on standard input, so that it shows in no command line or log.
- A key that reaches one bucket is a Cloudflare API token with the
  permission groups "Workers R2 Storage Bucket Item Read" and "Workers R2
  Storage Bucket Item Write" on the resource
  `com.cloudflare.edge.r2.bucket.ACCOUNT_default_BUCKET`. Its ID is the S3
  access key ID, and the SHA-256 of its value, in hex, is the secret.
  Making it needs a token that may manage API tokens and R2.
- Downloads from R2 carry no egress fee, unlike most object stores, so it
  suits data that many machines fetch again and again.
