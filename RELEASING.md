# Releasing

This repo was built inside a Home Assistant config repo. These steps publish it on its own. Replace `<you>` with your GitHub handle.

1. In the config repo, split the history out and clone it to a working copy. Every later step runs in that copy, never in the config repo, whose `origin` may be a live Home Assistant.
   ```sh
   git subtree split --prefix=ha-aoi-sig-mesh -b aoi
   git clone --branch aoi --single-branch "$PWD" /tmp/ha-aoi-sig-mesh
   git branch -D aoi
   cd /tmp/ha-aoi-sig-mesh && git branch -m main
   ```
2. If anything private ever entered the history, scrub it before the first push. Keep the replacements (`old==>new`, one per line, or `regex:...==>...`) in a file outside the repo, then run:
   ```sh
   pip install git-filter-repo
   git filter-repo --force --replace-text /path/to/replacements.txt
   ```
   Check that none of the old values is left: `git log -p | grep -ciE '<old value>'` prints 0.
3. Create an empty `ha-aoi-sig-mesh` repo on GitHub, with no README or licence, then push to it:
   ```sh
   git remote add origin git@github.com:<you>/ha-aoi-sig-mesh.git
   git push -u origin main
   ```
4. Put your handle in place of the owner placeholder, run the tests and push:
   ```sh
   grep -rl OWNER --exclude-dir=.git --exclude=RELEASING.md . | xargs sed -i '' 's/OWNER/<you>/g'
   python3 -m venv /tmp/aoi-release-venv
   /tmp/aoi-release-venv/bin/pip install pytest cryptography pyyaml
   /tmp/aoi-release-venv/bin/pytest -q
   git commit -am "chore(release): owner links" && git push
   ```
   On Linux, use `sed -i` without the `''`. `test_every_owner_link_agrees` checks that you caught them all.
5. On GitHub, set the repo description and the topics `home-assistant`, `hacs` and `bluetooth-mesh`. The HACS check in the Validate workflow fails until both are set.
6. Run the hardware checks and update the table under "What is tested on hardware" in the README to match what you saw. Then tag `v0.1.0` and publish a GitHub release with the CHANGELOG entry.
7. Check that both workflows are green, then add the repo to HACS as a custom repository and install it.
