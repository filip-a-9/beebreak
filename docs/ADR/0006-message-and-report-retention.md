# ADR 0006: Message and report retention

Status: Accepted (CONFIRMED 2026-10-14, M5). Source: ARCHITECTURE §10, §16
item 7; THREAT_MODEL T42, T44, T49.

## Context
Chat is not end-to-end encrypted, so bodies are readable by the operator while
they exist. Reports need evidence for moderation.

## Decision
Message bodies are deleted 24 h after the chat ends
(`BREAKROOM_MESSAGE_RETENTION_S`). Report evidence is deleted 90 days after the
report is resolved (`BREAKROOM_REPORT_RETENTION_S`); open reports are kept until
resolved. Ended queue entries and matches are deleted after 30 days
(`BREAKROOM_RETENTION_S`). The chat window always says "Not end-to-end
encrypted. Messages are deleted 24 h after the chat ends."

## Alternatives
The architecture proposed the 24-hour recovery window and longer, separately
disclosed report retention; no other values are recorded. End-to-end
encryption is deferred.

## Consequences
The operator can read bodies for up to 24 h after a chat ends (T49). Open
reports have no time limit, so an operator backlog is a retention risk.

## How it is tested
Purge tests, cascade tests and `test_a_full_chat_leaks_no_content`; checklist
M5-C16.

## Rollback
All durations are server configuration.
