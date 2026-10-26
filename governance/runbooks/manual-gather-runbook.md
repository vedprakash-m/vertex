# Manual Gather Runbook

**Scope:** Generic operator procedure. Examples use a fictional program.

1. Select an authorized program and review its source permissions and private
   freshness policy. Do not rely on an ambient program selection.
2. Run `vertex gather --program acme` using the deployment's approved identity.
   Do not put credentials in command arguments, logs or checked-in files.
3. Inspect the command result and committed gather manifest. Treat a failed,
   partial, stale or hash-invalid run as non-current; do not bypass a publish
   gate to turn it into trusted evidence.
4. Run `vertex doctor --edition acme_weekly` and resolve relevant failures
   before publishing. A doctor result is not source-access authorization.
5. Record operational evidence and any required follow-up privately.

Manual operation does not install scheduled tasks or establish a recurring
cadence. For automation, review the [scheduled tasks procedure](scheduled-tasks-runbook.md)
and obtain the deployment owner's approval first. Local launchers, task names,
credential account mappings and alert routes must remain private.
