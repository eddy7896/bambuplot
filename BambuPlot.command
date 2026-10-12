#!/bin/zsh
# Double-click in Finder to start BambuPlot and open it in your browser.
# Keep this Terminal window open while you use the app; close it (or press Ctrl+C) to quit.
cd "$(dirname "$0")" || exit 1
if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "BambuPlot needs uv. Install it from https://docs.astral.sh/uv/ and try again."
  read -k 1 "?Press any key to close."
  exit 1
fi
exec uv run bambuplot app "$@"
