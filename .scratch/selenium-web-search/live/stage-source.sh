set -eu
base=/home/samik/Projects/Jarvis
repo="$base/code/delete-note-options"
git -C "$repo" fetch "$base/code/web-source.bundle" main:refs/remotes/web-delivery/main
test ! -e "$base/code/selenium-web-live"
git -C "$repo" worktree add -b codex/selenium-web-live "$base/code/selenium-web-live" refs/remotes/web-delivery/main
cd "$base/code/selenium-web-live"
git cherry-pick f2d882e90daa5cf04647a8f54ab784a0de8b2e88
