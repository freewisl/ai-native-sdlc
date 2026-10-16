#!/bin/bash
# A minimal adopted repository: a git repo plus .sdlc/config.json, the file the plugin's SessionStart hook looks for.
# Runs in the run's temporary workspace (needs --scaffold).
set -eu
git init -q
git symbolic-ref HEAD refs/heads/main
git config user.email eval@example.invalid
git config user.name eval
mkdir -p .sdlc work src
cat > .sdlc/config.json <<'JSON'
{"version": 2, "language": "ko", "mode": "solo", "commands": {"build": "", "test": "", "lint": "", "run": ""}, "protect": {"default_branch": "main"}}
JSON
cat > src/login.html <<'HTML'
<form id="login">
  <input type="text" name="user" placeholder="ID">
  <input type="password" name="password" placeholder="Password">
  <button type="submit">Login</button>
</form>
HTML
printf '# demo\n' > README.md
git add -A
git commit -q -m init
