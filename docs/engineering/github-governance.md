# GitHub governance

This document defines the live GitHub control-plane target for Nexus AI. The NXS
machine-readable lifecycle remains authoritative for phase state; GitHub protection is
defense in depth so an accidental or unauthorized repository action cannot silently bypass
that lifecycle.

## Main branch

`main` must accept changes through pull requests. The canonical required checks are:
`state`, `quality`, `integration`, `docker`, `multiarch`,
`source-and-dependencies`, and `image`. Required checks are strict: a pull request must
be evaluated against the current protected base before merge.

Force pushes and branch deletion are prohibited. Review conversations must be resolved
before merge. Normal work must never use administrator bypass.

## Review model

The repository currently uses one trusted maintainer identity. Requiring one approving
review would create an impossible or misleading self-approval requirement, so the initial
server policy requires zero approving reviews while still requiring the pull-request path,
required checks, resolved conversations, CODEOWNERS, and explicit NXS human merge
authorization.

When a distinct trusted reviewer is available, increase required approvals to at least one
and enable dismissal of stale approvals. That change is a governance delta and must be
recorded rather than silently applied.

## CODEOWNERS

`.github/CODEOWNERS` assigns the repository and security-sensitive control surfaces to
`@devopssolutionsia`. Ownership is review routing and accountability; it does not replace
NXS lifecycle authorization or required CI/security evidence.

## Deliberately deferred controls

Signed commits are not yet mandatory because historical and current feature-branch commits
are not uniformly signed. Enabling enforcement without a signing migration would make the
existing governed workflow unusable. A future signing migration may strengthen this.

Linear history is not required because merge commits are part of canonical NXS closure and
integration evidence. Changing that model requires a separate governance decision.

## Break-glass

Administrator bypass is reserved for repository recovery when normal GitHub controls make
recovery impossible. A break-glass action must be explicitly authorized, documented with
reason and exact refs, followed by exact-main NXS CI/Security, and must never be used to
avoid a failing test, security finding, review requirement, or phase gate.

## Live verification

The desired settings are encoded in `.nxs/github-governance-policy.json`. H0-02 is not
complete merely because that file exists: the live GitHub settings must be applied and then
verified. If the available GitHub token cannot read an administrative setting, report it as
UNKNOWN rather than claiming PASS.

At H0-02 admission, the observed repository state was `main protected=false` with no
repository rulesets. That baseline is a gap to close, not an accepted final state.
