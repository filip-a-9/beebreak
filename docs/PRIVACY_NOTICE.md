# Breakroom privacy notice

> **Draft — not reviewed by a lawyer.**

This notice is for people testing Breakroom. It is written to the standard of
the EU General Data Protection Regulation (GDPR). It is also meant to cover
Serbia's Law on Personal Data Protection (ZZPL), which is modeled on the GDPR.

## Who is responsible

The controller is `<operator name>`. Contact: `<contact address>`.

## What we collect and why

| Purpose | Data | Lawful basis (PROPOSED) |
|---|---|---|
| Your account and signing in | Email address, nickname, sign-in codes (stored only as an HMAC), access and refresh tokens (stored only as hashes), device name and the times a device was created, last seen and connected, your consent choices | Contract (providing the service you signed up for) |
| Matching you with one other person | Only what you chose to share with a search: task category, stack tags, remaining-time bucket; queue and match records with times and the tags you share | Contract |
| Chat | Messages you send (body, time, sequence number), who took part, whether a participant was offline, how many messages each person sent | Contract |
| Blocking and reporting abuse | Blocks (who blocked whom), reports (reason, optional note up to 500 characters, status), the messages the reporter ticked as evidence | Legitimate interest (keeping the service safe and handling abuse) |
| Keeping out someone removed for abuse | If your account was disabled for abuse and then deleted: a keyed hash of your email address (not the address itself) and the time, kept for 1 year, so the address cannot be invited again without the operator deciding to | Legitimate interest (abuse prevention), PROPOSED for the lawyer review |
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
Account details are kept until the account is deleted. The keyed hash of the
email of a disabled account that was then deleted is kept for 1 year.

## Who receives it

- An email provider (SMTP) that sends your sign-in codes: to be named before
  the beta.
- A hosting provider that runs the server and database: to be named before the
  beta.

The other person in a chat sees your nickname, the tags and category you both
share, and your messages. They never see your email or account identifier.

Whether any provider is outside your country, and on what basis data would
be transferred, will be stated when the providers are named.

## Your rights

You can ask for access to your data (a copy), erasure, rectification of
inaccurate data, and object to processing based on legitimate interest. You can
complain to a supervisory authority. In Serbia that is the Commissioner for
Information of Public Importance and Personal Data Protection (Poverenik); in
the EU it is the authority of your country.

## How to use your rights

Email `<contact address>`. We reply within 14
days. An access request is answered with a JSON export of your own data; reports
about you appear only as a date, status and resolution date, without who
reported you or what they wrote. An erasure request deletes your
account and everything that refers to it. We cannot do it while you are in the
queue, in a live chat, or on either side of an open report; we will tell you and
do it once that is resolved. If your account was disabled, the operator reviews
the request first, because deleting it also removes the record of the ban. Rectification of a nickname or other data goes through
the same address.

## Appeals

If an operator disabled your account, email `<contact address>` with your
nickname. A person reviews the case and sends the outcome within 14 days.

## Changes

This notice lives in the project repository, so its history is visible there.
