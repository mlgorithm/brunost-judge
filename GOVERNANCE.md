# Governance

Brunost Judge is an independent open-source execution engine. Repository
maintainers are the GitHub users with maintainer access; they steward the public
contract, release process, and security response. Product integrations and
country/operator deployments remain separate from this repository's governance.

## Decisions

- Maintainers make routine decisions through issues and pull-request review.
- A public API, task manifest, result, callback, plugin, or security-boundary
  change requires maintainer approval and a compatibility review.
- A breaking change requires a written migration, a new versioned contract, and
  a release note. It must not be introduced through an undocumented behavior
  change.
- Maintainers may reject changes that weaken isolation, artifact immutability,
  credential scope, or task-data confidentiality.

## Roles

| Role | Responsibility |
| --- | --- |
| Contributor | Proposes focused, tested changes under the code of conduct. |
| Reviewer | Checks correctness, compatibility, tests, and documentation. |
| Maintainer | Merges reviewed changes, handles private security reports, and approves releases. |
| Release operator | Produces the tagged artifact/image, preserves evidence, and can roll back a deployment. |

At least two maintainers should review an official stable release when the
maintainer team size permits. A maintainer must not approve their own release
evidence alone for a production contest deployment.

## Community standards

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
Security-sensitive reports use the private process in [SECURITY.md](SECURITY.md),
not public issues. General help and feature requests follow [SUPPORT.md](SUPPORT.md).
