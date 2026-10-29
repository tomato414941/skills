---
name: agent-messaging
description: Send a message to another agent by typing into its tmux window, as a person would, and read its answer from the screen. The steps are the same whatever harness runs in the window. Use whenever you need to tell or ask another agent something.
---

# agent-messaging

- The agent takes what you type as its user's words. Look at its window with
  `tmux capture-pane` first, and do not type while it shows a question or an approval prompt or
  while its input already holds text. Never type an approval or a decision that is the user's to
  make; ask the user instead.
- Put the message on one line, since a line break can send it early. Type it with `send-keys -l`
  and send Enter on its own. Then look at the window: if the text still sits in the input, the
  Enter was taken with the typing, so wait a moment and send Enter again.
- For a window on another machine, pass the message on stdin so that no shell rewrites it:
  `printf '%s' "$message" | ssh <host> "tmux load-buffer -b msg - && tmux paste-buffer -d -b msg -t '<target>' && tmux send-keys -t '<target>' Enter"`
