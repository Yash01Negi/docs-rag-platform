# Database Backup and Restore Policy

## Scope

This policy covers the production PostgreSQL clusters that store customer documents and search indexes. Staging databases follow the same schedule but keep copies for seven days only.

## Backup schedule

A full snapshot of every production cluster is taken daily at 02:00 UTC. Write-ahead log segments are archived continuously to object storage, which allows point-in-time recovery to any second within the retention window. Snapshots are encrypted with a key that is separate from the database credentials.

## Retention

Daily snapshots are retained for 35 days. The snapshot taken on the first day of each month is kept for 13 months to satisfy audit requirements. Expired snapshots are deleted automatically; nobody deletes them by hand.

## Restore procedure

To restore a cluster, the on-call engineer opens a change ticket, provisions a new cluster from the chosen snapshot, and replays the archived log up to the requested time. The old cluster is never overwritten in place. After the restore, the engineer compares row counts for the documents and chunks tables before switching traffic.

## Restore drills

A restore drill is performed every quarter. The drill restores the largest production cluster into an isolated network and measures how long recovery takes. The target recovery time objective is four hours and the recovery point objective is fifteen minutes.
