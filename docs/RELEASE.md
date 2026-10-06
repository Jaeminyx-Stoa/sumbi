# Quiet public alpha

The first public alpha provides the capabilities documented as implemented in
the README. A public repository, a package-index release, and an announcement
are separate actions. Passing checks does not authorize publication.

## Gates

- Dogfood installation and collection, keeping logs and reports outside Git.
- Complete branch, pull request, review by a different model family, and
  maintainer merge for every release change. Do not merge your own pull request.
- Pass the unit suite on Python 3.11 and 3.13 and the repository's privacy check.
- Build a wheel and source archive; verify metadata, rebuild from the archive,
  and run source-fixture tests against the extracted archive.
- Confirm the README accurately separates implemented capabilities from plans,
  with supported agents and outcome sources stated explicitly.
- Audit the complete publishable Git history and hosted pull-request prose for
  private names, real data, developer paths, and secrets. Review the archive
  member lists as well as the current tree. Keep audit findings local.
- Obtain the owner's explicit approval before changing repository visibility,
  reserving a package name, uploading a distribution, or announcing a release.

## History and artifact audit

Work from a fully fetched checkout of the release candidate. Enumerate all
branches, tags, and pull-request refs that will be accessible after publication;
confirm each remote head is covered. Use `git rev-list --objects --all` and
`git cat-file --batch` to scan every reachable blob and commit message, including
files deleted from the latest tree. Scan path names and hosted descriptions,
comments, and review text separately. A shallow checkout or current-tree grep
alone does not establish a clean history.

Use a private list of prohibited names and a secret scanner that covers provider
tokens, private keys, credentials in URLs, and other secret formats. Never print
secret values or upload real logs as audit evidence. Record the candidate commit,
refs covered, scanner version and rules, counts, and unresolved findings in an
ignored local report. A clean automated scan still requires human review of
ambiguous findings and authored fixtures. If sensitive material was committed,
remove it from all publishable history and rotate exposed credentials before
rerunning the audit.

Build distributions from the reviewed candidate with no unreviewed source
changes. The archives must contain package code, documentation and synthetic
test assets only; exclude local logs, reports, caches, keys, and backups. Check
that the extracted source runs its tests and rebuilds an installable wheel.

## Owner-controlled publication

After the gates pass, present the exact candidate, artifact hashes, validation
results, known limitations, and proposed visibility or upload action to the
owner. Check package-name availability immediately before any reservation or
upload; availability is not a reservation. Use a protected publishing environment
and scoped credentials or trusted publishing. Never commit publishing secrets.
Record the released commit and artifacts after the owner authorizes publication.

## Later announcement

An announcement additionally needs an anonymized, evidence-backed before/after
example. Register the intervention before applying it and compare equal windows
using the same outcome source. Keep real ledgers and detailed findings local,
and review even aggregated output for identifying context before sharing it.
Roadmap features are not requirements merely because they appear in the design;
any capability promised for this release must pass its own acceptance gates.
