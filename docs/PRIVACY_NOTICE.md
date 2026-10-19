# Beebreak privacy notice

> **Template — not reviewed by a lawyer.**

**For operators.** The Beebreak project runs no server and collects no data.
Whoever runs a server is the operator and the controller of its data. Copy this
notice, fill in every field in angle brackets (`<operator name>`,
`<contact address>`, `<response time>` (the runbook assumes 14 days), `<email provider>`,
`<hosting provider>`, `<backup retention>`, `<transfers>`, `<changes>`,
`<supervisory authority>`), delete
this paragraph, and publish the result where your users can read it. The list
of fields and where they live is in [RUNBOOK.md](RUNBOOK.md), "Before you open
your server to others". Check the lawful bases and the retention periods
against the law that applies to you. When you have confirmed the lawful
bases, delete every "(PROPOSED)" marker and the sentence "The lawful bases
above are proposals and have not been confirmed."

This notice is for people who use a Beebreak server. It is written to the
standard of the EU General Data Protection Regulation (GDPR). Other data
protection laws that follow the GDPR will be close to it, but the operator must
check.

"We" in this notice means the operator.

## Who is responsible

The controller is `<operator name>`. Contact: `<contact address>`.

## What we collect and why

| Purpose | Data | Lawful basis (PROPOSED) |
|---|---|---|
| Your account and signing in | Email address (or, if you sign in with GitHub, only your numeric GitHub user id; no email, name or username), nickname, sign-in codes (stored only as an HMAC), access and refresh tokens (stored only as hashes), device name and the times a device was created, last seen and connected, your consent choices | Contract (providing the service you signed up for) |
| Matching you with one other person | Only what you chose to share with a search: task category, stack tags, remaining-time bucket; queue and match records with times and the tags you share | Contract |
| Chat | Messages you send (body, time, sequence number), who took part, whether a participant was offline, how many messages each person sent | Contract |
| Blocking and reporting abuse | Blocks (who blocked whom), reports (reason, optional note up to 500 characters, status), the messages the reporter ticked as evidence | Legitimate interest (keeping the service safe and handling abuse) |
| Keeping out someone removed for abuse | If your account was disabled for abuse and then deleted: a keyed hash of your email address or GitHub user id (not the address or id itself) and the time, kept for 1 year, so the address cannot be invited again without the operator deciding to | Legitimate interest (abuse prevention), PROPOSED; check before publishing |
| Protecting the service from abuse and attacks | Your IP address (or the address a trusted proxy forwards): held in the server's memory only, as a counter key that limits sign-in attempts and connections, dropped at the next request of the same kind after its 15-minute window ends, so on a quiet server it can stay longer, with no fixed upper bound, and always when the server restarts; and in the web proxy's error log, which can include the address, the host name and the page requested, kept in size-limited rotating files (the oldest is overwritten as new entries arrive, so there is no fixed number of days). The server's own logs do not contain it | Legitimate interest (security and abuse prevention) |
| Appeals, access and erasure requests | The email you send and our reply | Legitimate interest, and legal obligation where it applies |

Your device name is a generic default (`WSL device`) that you can change
before signing in, and the privacy preview in the app shows it before it is
sent. Your computer's hostname is never sent.

We do not collect or upload your prompts, source code, commands, file paths,
repository names or Claude session identifiers. These stay on your machine
(see [DATA_HANDLING.md](DATA_HANDLING.md)).

Chat is **not end-to-end encrypted**. The operator can read messages while
they exist, and can read report notes and report evidence. The admin CLI has no
command that reads a chat, and a report shows the operator only the note and the
messages the reporter ticked.

The lawful bases above are proposals and have not been confirmed.

## How long we keep it

See the per-item table in [DATA_HANDLING.md](DATA_HANDLING.md). In short:
message bodies 24 hours after a chat ends; queue entries and match records 30
days after they end; report evidence 90 days after a report is resolved (open
reports until resolved); sign-in challenges 24 hours, refresh tokens 7 days
after they expire, and expired, redeemed or revoked invites 30 days after they end.
IP addresses in the server's memory are dropped at the next request of the same kind after their 15-minute window ends (on a quiet server that can be later, with no fixed upper bound), and always on restart. Account details are kept until the account is deleted. The keyed hash of the
email or GitHub user id of a disabled account that was then deleted is kept for 1 year.

## Who receives it

- An email provider (SMTP) that sends your sign-in codes: `<email provider>`.
  Not used if the server offers only GitHub sign-in.
- GitHub, if you choose "Sign in with GitHub": GitHub learns that you signed in
  to Beebreak, and from which address the code was entered. Beebreak asks for
  no permissions, receives your numeric user id once, and does not keep GitHub's
  access token or ask GitHub for anything afterwards.
- A hosting provider that runs the server and database: `<hosting provider>`.
- The operator's backup storage, if any, kept for `<backup retention>`. Backups
  hold the same data as the database, including rows that were deleted after
  the backup was taken.

The other person in a chat sees your nickname, the tags and category you both
share, and your messages. They never see your email or account identifier.

`<transfers>`: say whether any provider is outside your country, and on what
basis data is transferred.

## Your rights

You can ask for access to your data (a copy), erasure, rectification of
inaccurate data, and object to processing based on legitimate interest. You can
complain to a supervisory authority: `<supervisory authority>` (in the EU, the
authority of your country).

## How to use your rights

Email `<contact address>`. We reply within `<response time>`. An access request is answered with a JSON export of your own data; reports
about you appear only as a date, status and resolution date, without who
reported you or what they wrote. An erasure request deletes your
account and everything that refers to it, including reports other people
filed about you and the message copies attached to them, resolved ones too
(PROPOSED; check before publishing). We cannot do it while you are in the
queue, in a live chat, or on either side of an open report; we will tell you and
do it once that is resolved. If your account was disabled, the operator reviews
the request first, because deleting it also removes the record of the ban. Rectification of a nickname or other data goes through
the same address.

## Appeals

If an operator disabled your account, email `<contact address>` with your
nickname. A person reviews the case and sends the outcome within `<response time>`.

## Changes

`<changes>`: say where this notice is published and how you tell users when it changes.
