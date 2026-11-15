# Private local configuration and public development

Application source belongs in this upstream repository. Documents, credentials, user/access rules, cloud state, logs, backups, and machine-specific settings belong to each installation. Keep the older private repository and its Git history separate.

## Set up a development checkout

Start a separate source-only development checkout from this repository, not an old private clone. Leave the customized production checkout and its private files in place:

```sh
git clone git@github.com:KTV02/PdfSearchEngine.git
cd PdfSearchEngine
python3 scripts/install_privacy_guards.py
```

The installer verifies a pinned Gitleaks release checksum and enables this checkout's commit, commit-message, and push hooks. It does not change global Git settings, deploy services, or replace existing custom hooks. New clones need this setup; hooks are not automatically enabled by Git.

Use a reviewed public author alias listed in `.privacy-policy.json` and your own GitHub noreply email in this checkout's local Git configuration. The guard rejects unreviewed names and personal email addresses without printing them. Review policy updates for additional contributors rather than disabling this check.

## Keep the customized installation local

Use `app/.env`, root `compose.override.yaml`, or the supported ignored override names for deployment changes. Keep new private output under `app/data/`, `.local/`, or `.private/`, or outside the checkout. Existing legacy private locations remain ignored too. Both YAML extensions for conventional Compose override files are ignored at every directory level.

Edit shared service defaults in `app/docker-compose.yml` only when that change is intended for everyone. Put local paths, identities, endpoint addresses, credentials, capability changes, and extra mounts in the local files instead. Do not save rendered Compose or container-inspection output in a public source file.

Private document/archive/key/database files and common backup suffixes are ignored. The publication check also blocks private paths that were force-added or were already tracked. Generate synthetic test documents in temporary directories rather than committing real documents or screenshots.

After changing local settings or adding private documents, refresh the optional local deny profile:

```sh
python3 scripts/privacy_profile.py --refresh-local
```

This reads local configuration and this checkout's labeled Docker runtime, when available, and remembers salted fingerprints of private values and document names. It does not extract document text, send values elsewhere, start services, or store plaintext credentials. The profile stays under Git's internal directory with owner-only file permissions. It is never committed or copied into the supported build contexts. It is an extra guard for this installation; CI does not need or receive its private profile. Unmounted/unreadable locations may require a separately supplied local profile. Refreshing does not replace review.

## Develop and push features

```sh
git switch -c feature/short-description
# Edit reusable source and synthetic tests.
git add --all
python3 scripts/privacy_check.py --staged
git diff --cached --stat
# Review the staged content privately, especially new examples and prose.
git commit -m "Describe the reusable change"
git push -u origin HEAD
```

The commit hook checks the entire staged snapshot; ignored local configuration does not need to be removed. The push hook checks all locally reachable history and rejects an independent/older root, private paths, suspicious content, or unreviewed author identities. It also runs full-history credential scanning. Changes on multiple local branches are checked, so move unrelated private experiments to a separate private repository rather than bypassing the hook.

The `Publication privacy` Actions workflow repeats public checks upstream and tests real Docker context exports with synthetic private files. A repository owner should require that check in GitHub branch protection/rulesets before public collaboration; the workflow alone does not prevent a direct push. Do not bypass hooks or force-add private files. Changes to `.privacy-policy.json`, the scanner configuration, hooks, or workflow need deliberate review.

Normal new Python, frontend, test, and documentation files fit the reviewed public path patterns. A new file type or source layout needs an explicit policy update and privacy review. No automatic scanner can recognize every personal fact in prose, an arbitrary encoded secret, or an intentionally weakened policy.

## Add source files to Docker builds explicitly

The repository root `.dockerignore` excludes everything. A root build context intentionally cannot package the installation. Use Compose's existing service contexts, or these explicit contexts from the repository root:

```sh
docker build -f app/backend/Dockerfile app/backend
docker build -f app/email_processor/Dockerfile app/email_processor
```

Each supported context lists exact source inputs. When adding a backend Python module, add its filename to `app/backend/.dockerignore` and its `backend/` path to `app/.dockerignore`. Do the equivalent for a new mail-processing module; add new application test modules to `app/.dockerignore`. The staged check catches a missing or overly broad list. Do not replace the lists with `!*.py` or broad `COPY` exceptions for a private deployment.

Build/distribute images from a clean source-only checkout. Build arguments, Docker image environment settings, and Compose files copied into images must not contain real credentials. An ignored local override can change build contexts, so review its resolved configuration privately. Root-context protection cannot secure an arbitrary unrelated directory selected in an override.

A push does not rebuild or restart the production stack. However, files bind-mounted into running containers, including frontend assets, can affect the live application as soon as they are edited or pulled in the production checkout. Use the separate development checkout for feature work and isolated tests. Existing locally customized settings remain local when pulling shared source changes; do not replace them with examples. Review configuration/mount compatibility and plan deployment separately.

## Before making the repository or an image public

Run the full-history check:

```sh
python3 scripts/privacy_check.py --history
```

Review all references and staged files, new documentation/examples, metadata, image layers, releases, and artifacts. Keep the old private repository/history and private local backups separate. Current source exclusions do not clean earlier Docker images or recall earlier downloads. Do not distribute the previously identified sensitive images; rebuild and inspect replacements separately. Resolve the production test-bypass finding separately before treating production as hardened. Repository visibility changes require an explicit decision after these checks.
