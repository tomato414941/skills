---
name: agent-messaging
description: Send a message to another agent by typing into its tmux window, as a person would, and read its answer from its transcript file. The steps are the same whatever harness runs in the window. Use whenever you need to tell or ask another agent something.
---

# agent-messaging

- Read its conversation history from its transcript file, not only its screen.
- Say who you are.
- Faint text in the input is a suggestion, not something typed. Plain `capture-pane` does not
  set it apart; with `-e` it shows after `ESC[2m`.
- Write the message on one line, type it with `send-keys -l`, and send Enter on its own. If the
  text stays in the input, wait a moment and send Enter again.
- For a window on another machine, pass the message on stdin so that no shell rewrites it:
  `printf '%s' "$message" | ssh <host> "tmux load-buffer -b msg - && tmux paste-buffer -d -b msg -t '<target>' && tmux send-keys -t '<target>' Enter"`
