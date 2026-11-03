## ADDED Requirements

### Requirement: Idle sessions are compressed automatically
The system SHALL compress Claude Code and Codex sessions that have not been reopened for the configured
number of days, 30 by default, including hidden sessions and whatever their origin. It SHALL run in the
background when the browser starts and SHALL NOT delay it.

#### Scenario: Idle session
- **WHEN** a session's last activity is more than 30 days old
- **THEN** it is compressed and stays listed under the compressed filter

#### Scenario: Hidden session
- **WHEN** a hidden session's last activity is more than 30 days old
- **THEN** it is compressed like any other and stays hidden

#### Scenario: Recently active
- **WHEN** a session's transcript changed within 30 days
- **THEN** it is left uncompressed

#### Scenario: Open session
- **WHEN** an idle session is open
- **THEN** it is left uncompressed

#### Scenario: Harness without transcript files
- **WHEN** a session belongs to a harness that cannot be compressed
- **THEN** it is skipped

#### Scenario: One failure
- **WHEN** one session cannot be compressed
- **THEN** it stays as it is, the failure is logged, and the run continues

### Requirement: Reopening restarts the clock
The system SHALL treat opening a session from `sessions`, or decompressing it, as activity even if no
transcript content changes.

#### Scenario: Opened without writing
- **WHEN** an old session was opened from `sessions` within 30 days
- **THEN** it is left uncompressed

#### Scenario: Decompressed
- **WHEN** an old session was decompressed within 30 days
- **THEN** it is not compressed again until 30 more days pass

#### Scenario: Opened during a run
- **WHEN** a session is opened after a run started but before the run reaches it
- **THEN** that session is skipped

### Requirement: A session and its subagents are idle together

A session and its subagents SHALL count as one unit: none of them SHALL be compressed automatically unless every one of them qualifies. A subagent whose parent is no longer listed SHALL stand alone.

#### Scenario: Parent reopened

- **WHEN** a session was opened recently and its subagent's log is old
- **THEN** neither is compressed

#### Scenario: Subagent still working

- **WHEN** a subagent's log changed recently and its parent's transcript is old
- **THEN** neither is compressed

#### Scenario: Orphaned subagent

- **WHEN** a subagent is idle and its parent is no longer listed
- **THEN** the subagent is compressed

### Requirement: Compression cannot lose history

A compressed copy SHALL be replaced only by a file containing all of it. A file another process has open SHALL NOT be compressed. A damaged compressed copy SHALL NOT be restored or removed. A run SHALL stop when it cannot tell which sessions are open.

#### Scenario: Short plain file beside a full copy

- **WHEN** a plain transcript does not contain the compressed copy beside it
- **THEN** compressing or decompressing refuses, reports it, and keeps both files

#### Scenario: Live writer

- **WHEN** another process has the transcript open
- **THEN** compression refuses, and everything that process writes is kept

#### Scenario: Truncated copy

- **WHEN** a compressed copy stops part way through
- **THEN** decompressing fails and the copy is kept

#### Scenario: Restored meanwhile

- **WHEN** something restores a transcript while a decompress is running
- **THEN** the file already there is kept

#### Scenario: Partial session

- **WHEN** a session's main file cannot be compressed after its subagent logs were
- **THEN** those logs are restored

#### Scenario: Log left behind

- **WHEN** a session is opened with a compressed subagent log left over
- **THEN** that log is restored too

#### Scenario: Liveness unknown

- **WHEN** a run cannot establish which sessions are open
- **THEN** it stops and compresses nothing further

#### Scenario: Terminal commands

- **WHEN** a compress command runs from a terminal
- **THEN** it uses freshly discovered sessions and their live state, not the browser's catalog

### Requirement: One run at a time
The system SHALL run at most one idle compression at a time across all `sessions` windows, SHALL start
one at most every six hours, and SHALL recover from a run that died.

#### Scenario: Several windows
- **WHEN** several `sessions` windows start together
- **THEN** only one run happens

#### Scenario: Run died
- **WHEN** a lock is left behind by a process that no longer exists
- **THEN** the next run proceeds

### Requirement: Idle compression can be previewed and turned off
The system SHALL provide a command that runs idle compression now, a dry run that changes nothing, and
a setting for the number of days where 0 turns automatic compression off.

#### Scenario: Dry run
- **WHEN** `sessions --compress-idle --dry-run` runs
- **THEN** it reports what would be compressed and no file changes

#### Scenario: Turned off
- **WHEN** `compress_after_days` is 0
- **THEN** no automatic run starts and `--compress-idle` reports that it is off

#### Scenario: Invalid setting
- **WHEN** `compress_after_days` is negative or not a whole number
- **THEN** the default of 30 applies
