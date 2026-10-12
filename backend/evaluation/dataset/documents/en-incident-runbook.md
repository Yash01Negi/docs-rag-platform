# Incident Response Runbook

## Severity levels

SEV1 means the product is unavailable or customer data may be exposed. SEV2 means a major feature such as search or upload is degraded for many users. SEV3 covers minor bugs that have a workaround.

## Paging and escalation

SEV1 and SEV2 incidents page the primary on-call engineer immediately. If the page is not acknowledged within ten minutes, it escalates to the secondary on-call engineer and then to the engineering manager.

## Roles during an incident

Every SEV1 incident has an incident commander who coordinates the response, a communications lead who updates the status page every thirty minutes, and responders who investigate. The incident commander does not debug; their job is to keep the response organized.

## Postmortems

A blameless postmortem is written for every SEV1 and SEV2 incident within five business days. The postmortem lists the timeline, the root cause, what went well, and action items with owners and due dates.

## Customer communication

Customers affected by a SEV1 incident receive an email summary within one business day after the incident is resolved.
