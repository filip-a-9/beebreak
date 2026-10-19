# ADR 0017: One GDPR-grade privacy notice

Status: Accepted (CONFIRMED 2026-10-16, M6). The lawful bases inside the notice
are PROPOSED. Amended by [ADR 0021](0021-no-hosted-instance.md): the notice is
a template each operator completes. Source: M6 README, M6-09.

## Context
Users of a server may be in several jurisdictions. Several national laws are
modeled on the GDPR.

## Decision
One privacy notice written to the GDPR standard, as a template. The operator of
a server fills in the controller name, contact address and providers, and is
the controller. The maintainer collects no data and is not a controller.

## Alternatives
None recorded.

## Consequences
The notice is marked "Template — not reviewed by a lawyer." The controller name,
contact address, SMTP provider and host are fields the operator fills in. An
operator in a jurisdiction with other rules has to check them.

## How it is tested
Not by code. Links and placeholders are checked at merge.

## Rollback
Not recorded.
