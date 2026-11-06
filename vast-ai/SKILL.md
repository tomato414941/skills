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
- `rent` and `ssh-config` need the private key file that VAST_SSH_KEY_FILE
  names: the Vast.ai key that INFRA.md names for the machine you are on. Do
  not make another key. Hand it over from Foundation as a file, which lasts as
  long as the command:
  `foundation exec --inputs '[{"name": "VAST_SSH_KEY_FILE", "source": {"id": "VARIABLE"}, "format": "file"}]' -- COMMAND`.
  `rent` gives its public half to the machine it rents, and only to that
  machine. The Vast.ai API key comes from `.env` beside the script, or from
  VAST_API_KEY.
- `rent` checks the machine over ssh with that key, the way the work will
  reach it, then writes how to reach it as an ssh configuration and prints its
  path as `ssh_config`. Do the work through it, with the same key:
  `ssh -F PATH -i "$VAST_SSH_KEY_FILE" -o IdentitiesOnly=yes vast-INSTANCE`,
  and `rsync -e` with the same ssh. Its address shows nowhere.
  `vast-rental ssh-config INSTANCE` writes the file again.
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
  whose GPU multiplies a fixed pair of matrices wrongly or differently twice,
  that is slower than checked machines with the same GPU, that downloads
  slower than `--min-download` MB/s (10 by default), or whose traffic leaves
  from another country than its listing names.
- Rent only machines whose driver supports the image's CUDA version, such as
  `--query '{"cuda_max_good": {"gte": 12.8}}'` for a CUDA 12.8 image.
