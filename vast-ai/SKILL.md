---
name: vast-ai
description: Rent a GPU machine on Vast.ai, run work on it, and delete it. Use whenever a task rents or manages Vast.ai instances.
---

# vast-ai

- Rent, use and return Vast.ai machines through `vast-rental`
  (github.com/tomato414941/vast-rental; its `install.sh` puts it on PATH).
  Every rental goes into its ledger, which all sessions on the workstation
  share, so that a machine that failed for anyone is not rented again.
- `vast-rental offers GPU` shows the offers in the order that `rent` tries
  them. `vast-rental rent GPU --image IMAGE --label LABEL --hours HOURS`
  rents the cheapest suitable machine, checks it at once and sends it back if
  it fails, until one passes or `--tries` machines have failed, and prints the
  instance it kept as JSON.
- `rent` and `run` need an SSH agent that holds exactly one key, whose public
  half each machine is given:
  `foundation ssh-key agent KEY -- vast-rental rent ...`, where KEY is the
  workstation's Vast.ai key that INFRA.md names. Every session on the
  workstation uses that one key, so do not make another. The agent drops the
  key after 15 minutes, so wrap each later `run` again. The Vast.ai API key
  comes from `.env` beside the script, or from VAST_API_KEY.
- Give `--hours` as the longest the work may take: a guard returns the machine
  at that deadline even if the session that rented it has ended.
  `vast-rental extend INSTANCE HOURS` moves the deadline.
- When the work ends, record how it ran (`vast-rental note INSTANCE TEXT
  --values JSON`), then return the machine (`vast-rental return INSTANCE`).
  If the work ran much slower than on other machines with the same GPU, say
  so in the note.
- `rent` keeps to machines that Vast.ai has verified; `--unverified` lets in
  unverified ones too. It sends back a machine that has not begun to load its
  image after five minutes, whose GPU holds memory or has its power capped,
  that is slower than checked machines with the same GPU, that downloads
  slower than `--min-download` MB/s (10 by default), or whose traffic leaves
  from another country than its listing names.
- Rent only machines whose driver supports the image's CUDA version, such as
  `--query '{"cuda_max_good": {"gte": 12.8}}'` for a CUDA 12.8 image.
