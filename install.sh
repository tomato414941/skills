#!/usr/bin/env bash
# Link every skill in this repository into Claude Code's and Codex's skill
# folders, and remove links to skills that have left it.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
status=0
for target in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
  mkdir -p "$target"
  for link in "$target"/*; do
    if [[ -L "$link" && "$(readlink "$link")" == "$repo"/* && ! -e "$link" ]]; then
      rm "$link"
      echo "removed $link"
    fi
  done
  for skill in "$repo"/*/SKILL.md; do
    [[ -e "$skill" ]] || continue
    source="$(dirname "$skill")"
    link="$target/$(basename "$source")"
    if [[ -L "$link" && "$(readlink "$link")" == "$source" ]]; then
      continue
    fi
    if [[ -e "$link" || -L "$link" ]]; then
      echo "skipped $link: something else is already there" >&2
      status=1
      continue
    fi
    ln -s "$source" "$link"
    echo "linked $link"
  done
done
exit "$status"
