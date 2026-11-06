---
name: foundation
description: Hand the credentials that Foundation holds to the commands that need them, so that no program has to know Foundation. Use whenever a task needs an API key, a token or an SSH key, and whenever writing a program that needs one.
---

# foundation

- Programs never call Foundation. A program takes its credentials as any
  program would, from environment variables, files or the SSH agent, and the
  command that runs it is wrapped with Foundation.
- `foundation exec --inputs '[{"name": "VAR", "source": {"id": "ITEM", "output": "OUTPUT"}}]' -- COMMAND`
  runs COMMAND with VAR set to the item's value; `output` picks one of a
  connection's outputs. With `"format": "file"`, VAR names a file that holds
  the value and is deleted when COMMAND ends.
- Of the caller's environment, COMMAND gets only PATH, HOME, LANG, LC_ALL, TZ,
  TERM and TMPDIR besides the inputs. Pass anything else as arguments.
- Values that reach COMMAND's output come out as `[redacted]`. Never print a
  value, or any part of one, yourself.
- `foundation ssh-key agent KEY -- COMMAND` runs COMMAND with SSH_AUTH_SOCK
  naming a temporary agent that holds that one key, and passes the whole
  environment on. To give a command both, put the agent inside exec:
  `foundation exec --inputs ... -- foundation ssh-key agent KEY -- COMMAND`.
  The other way round, exec drops SSH_AUTH_SOCK.
- The agent drops the key 15 minutes after COMMAND starts. Connections opened
  before then stay open; wrap a later connection again.
- exec runs COMMAND in a process group of its own and passes SIGINT and
  SIGTERM to it. A process that COMMAND starts in a session of its own keeps
  running after exec ends, with the values it was given. `--timeout` does not
  limit a command that exec runs.
- Item IDs are not secret, but they belong to this environment: keep them out
  of repositories. Take them from INFRA.md or from the user.
- Making or storing a credential, as `foundation ssh-key create --store` and
  `foundation keep` do, is the user's to do: give them the command to run
  with `!`.
