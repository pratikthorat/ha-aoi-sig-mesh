# Releasing

This repo was built inside a Home Assistant config repo. Steps 1–9 publish it on its own, once. "Later releases" covers every release after that. You need `git`, `gh` (logged in to your own account) and `git-filter-repo`.

1. In the config repo, split the history out and clone it to a working copy. Every later step runs in that copy, never in the config repo, whose `origin` may be a live Home Assistant.
   ```sh
   git subtree split --prefix=ha-aoi-sig-mesh -b aoi
   git clone --branch aoi --single-branch "$PWD" ~/ha-aoi-sig-mesh
   git branch -D aoi
   cd ~/ha-aoi-sig-mesh && git branch -m main
   ```
2. If anything private ever entered the history, scrub it before the first push. Keep the replacements (`old==>new`, one per line, or `regex:...==>...`) in a file outside the repo, then run:
   ```sh
   git filter-repo --force --replace-text /path/to/replacements.txt
   ```
   Check that none of the old values is left: `git log -p | grep -ciE '<old value>'` prints 0.
3. Put your GitHub handle in place of the owner placeholder, then run the tests. Use double quotes, so `$ME` is filled in.
   ```sh
   ME=$(gh api user --jq .login) && echo "ME=[$ME]"
   grep -rl OWNER --exclude-dir=.git --exclude=RELEASING.md . | xargs sed -i '' "s/OWNER/$ME/g"
   python3 -m venv /tmp/aoi-release-venv
   /tmp/aoi-release-venv/bin/pip install pytest cryptography pyyaml
   /tmp/aoi-release-venv/bin/pytest -q
   git commit -am "chore(release): owner links"
   ```
   On Linux, use `sed -i` without the `''`. `test_every_owner_link_agrees` checks that you caught them all.
4. Create the repo under your account and push:
   ```sh
   gh repo create ha-aoi-sig-mesh --public \
     --description "Local Bluetooth SIG Mesh lights and plugs for Home Assistant, no cloud or hub" \
     --source=. --remote=origin --push
   gh repo edit --enable-issues --add-topic home-assistant --add-topic hacs --add-topic bluetooth-mesh
   ```
   The HACS check fails until the description and topics are set.
5. Turn on private vulnerability reporting, which `SECURITY.md` and the issue form point to:
   ```sh
   gh api -X PUT "repos/$ME/ha-aoi-sig-mesh/private-vulnerability-reporting"
   ```
6. Check that the Tests and Validate workflows are green: `gh run list`.
7. Add the repo to HACS as a custom repository, install it, and run the hardware checks. Update the table under "What is tested on hardware" in the README to match what you saw, then commit and push.
8. Tag the release and publish it. The Release workflow fails if the tag is not `v` plus the manifest version, or `CHANGELOG.md` has no entry for it.
   ```sh
   git tag -a v0.1.0 -m "v0.1.0" && git push origin v0.1.0
   gh release create v0.1.0 --title "v0.1.0" --notes "First release. See CHANGELOG.md."
   gh run list --workflow=release.yml
   ```
9. Ask for the repo to be added to the HACS default list, so people find it by searching in HACS. The rules are at https://hacs.xyz/docs/publish/include: the HACS action must pass with nothing ignored, hassfest must pass, and there must be a full release (step 8). Submit from your personal account, not an organisation:
   ```sh
   gh repo fork hacs/default --clone && cd default
   git checkout -b add-aoi-sig-mesh upstream/master
   ```
   Add `"<you>/ha-aoi-sig-mesh"` to the `integration` file in its alphabetical place, not at the end, then:
   ```sh
   python3 -c "import json; d = json.load(open('integration')); assert d == sorted(d, key=str.lower)"
   git commit -am "Add <you>/ha-aoi-sig-mesh" && git push -u origin add-aoi-sig-mesh
   gh pr create --repo hacs/default --web
   ```
   Fill in every part of the pull request template, truthfully, or the PR is closed. Reviews take months. Once it is merged, change the README's HACS badge from `HACS-Custom` to `HACS-Default`.

## Later releases

1. Put the new version in `custom_components/aoi_sig_mesh/manifest.json` and add a `## <version>` entry to `CHANGELOG.md`. Commit, and push to `main`.
2. Wait for the Tests and Validate workflows to go green.
3. Tag and publish as in step 8, with the new version. HACS offers the update to everyone who installed it.
