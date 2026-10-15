# Releasing

This repo was built inside a Home Assistant config repo. These steps publish it on its own.

1. In the config repo, split the history out:
   `git subtree split --prefix=ha-aoi-sig-mesh -b aoi`
2. Create an empty `ha-aoi-sig-mesh` repo on GitHub, then push the branch to it as `main`.
3. Put your GitHub handle in place of `OWNER` everywhere:
   `grep -rl OWNER --exclude-dir=.git . | xargs sed -i '' 's/OWNER/<your-handle>/g'`
   (On Linux, use `sed -i` without the `''`.)
   Then run `pytest -q`: `test_every_owner_link_agrees` checks that you caught them all. Commit.
4. On GitHub, set the repo description and the topics `home-assistant`, `hacs` and `bluetooth-mesh`. The HACS action checks both.
5. Run the hardware checks and update the table under "What is tested on hardware" in the README to match what you saw. Then tag `v0.1.0` and publish a GitHub release with the CHANGELOG entry.
6. Check that both workflows are green, then add the repo to HACS as a custom repository and install it.
