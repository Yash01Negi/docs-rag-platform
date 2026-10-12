# SnapKeep Backup Agent Release Notes

## Version 4.2

The backup agent now compresses backup archives with zstd, which makes backups about 30 percent smaller. The restore wizard shows a progress bar during long restores.

## Version 4.1

Scheduled backups can now run every hour. A new command lists every backup stored on the local disk. Restoring a single file from a backup no longer requires restoring the whole archive.

## Version 4.0

The agent drops support for 32-bit Windows. Backup encryption keys are now stored in the operating system keychain.

## Known issues

Restoring very large photo libraries from a backup can be slow on network drives.
