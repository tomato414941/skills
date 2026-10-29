---
name: agent-messaging
description: Send a message to another agent by typing into its tmux window, as a person would, and read its answer from the screen. The steps are the same whatever harness runs in the window. Use whenever you need to tell or ask another agent something.
---

# agent-messaging

- Address a window as `session:window`; `tmux list-windows -a -F '#{session_name}:#{window_name}'`
  lists them. Window names may contain colons.
- Look at the window first with `tmux capture-pane -p -t <target>`. Do not type while it shows a
  question or an approval prompt, or while its input already holds text: what you type and the
  Enter would answer it.
- The agent there takes what you type as its user's words. Never type an approval or a decision
  that is the user's to make; ask the user instead.
- Put the message on one line, since a line break can send it early. Type it with
  `tmux send-keys -t <target> -l '<message>'`, then send Enter on its own with
  `tmux send-keys -t <target> Enter`; `-l` types the text as it is.
- For a window on another machine, pass the message on stdin so that no shell rewrites it:
  `printf '%s' "$message" | ssh <host> "tmux load-buffer -b msg - && tmux paste-buffer -d -b msg -t '<target>' && tmux send-keys -t '<target>' Enter"`
- Read the answer from the window with `tmux capture-pane`; if the agent is still working, look
  again later.
