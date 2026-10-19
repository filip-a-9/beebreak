# ADR 0021: No hosted instance

Status: Accepted (CONFIRMED 2026-10-19). Amends
[ADR 0013](0013-appeals-and-data-requests-by-email.md) and
[ADR 0017](0017-single-gdpr-grade-privacy-notice.md); see also
[ADR 0001](0001-apache-2-licence.md).

## Context
The first plan was a restricted hosted beta run by the maintainer, with a closed
cohort (M7). That would make the maintainer a data controller with a mailbox, a
privacy notice, backups, abuse handling and a response time to keep.

## Decision
Beebreak ships as open source only. The maintainer runs no instance, not even
a demo with other people on it, and collects no data. Whoever wants a network
runs their own server from `compose.prod.yaml` and is its operator.

## Alternatives
A hosted beta run by the maintainer (the earlier plan); a hosted demo. Both
rejected: they bring legal and operating duties the project does not take on.

## Consequences
- The privacy notice is a template. The operator fills in the controller,
  contact address and providers, and is responsible for it.
- Fields an operator must fill in are listed in the RUNBOOK ("Before you open
  your server to others").
- Parts of the server were only tested locally. They are listed in the RUNBOOK
  ("Untested on a real host"); an operator checks them on the real host.
- No lawyer has reviewed the privacy notice, and the maintainer does not plan a
  review. Operators who need one commission it for their own jurisdiction.
- Security reports about Beebreak's code go to the project (GitHub private
  vulnerability reporting). Abuse reports and data requests on a server go to
  that server's operator.
- The closed beta (M7) is dropped.

## How it is tested
Not by code. Links and fields are checked at merge.

## Rollback
Not recorded.
