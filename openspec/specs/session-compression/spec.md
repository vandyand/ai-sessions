# session-compression Specification

## Purpose

Keep every Claude Code and Codex session by compressing idle ones instead of deleting them, while
they stay listed, filterable and recoverable from `sessions`.

## Requirements

### Requirement: Compression never loses a transcript
The system SHALL remove an original transcript only after a compressed copy has been read back and
found byte-identical, and only if nothing changed the original during compression. Any failure SHALL
leave the original in place.

#### Scenario: Round trip
- **WHEN** a transcript is compressed and then decompressed
- **THEN** the result is byte-identical to the original and has its original modification time

#### Scenario: Written to during compression
- **WHEN** a harness appends to a transcript while it is being compressed
- **THEN** compression fails with a message and the transcript, with the new content, stays in place

#### Scenario: File in use
- **WHEN** the original cannot be removed because another process holds it
- **THEN** the transcript stays uncompressed and no compressed copy is left behind

#### Scenario: Interrupted run
- **WHEN** compression stops after the compressed copy was published but before the original was removed
- **THEN** the plain transcript remains the session's content

#### Scenario: Corrupt compressed file
- **WHEN** a compressed transcript cannot be decompressed
- **THEN** an error is reported and the compressed file is left untouched

#### Scenario: Not enough disk space
- **WHEN** decompressing would leave too little free disk space
- **THEN** an error is reported before anything is written

### Requirement: Plain transcripts take precedence
When a session exists both as a plain transcript and as a compressed copy, the system SHALL treat the
plain transcript as the session's content and list the session once, as uncompressed.

#### Scenario: Both forms present
- **WHEN** `x.jsonl` and `x.jsonl.zst` both exist
- **THEN** one uncompressed session is listed, read from `x.jsonl`

### Requirement: Compressed sessions are listed
The system SHALL discover compressed Claude Code and Codex sessions, including subagent transcripts,
with the same title, directory, dates and activity counts as when uncompressed, including transcripts
written by a harness's own compressor.

#### Scenario: Compressed by a harness
- **WHEN** a Codex rollout exists only as `.jsonl.zst` and its database lists the plain path
- **THEN** the session is listed as compressed with its title and activity counts

#### Scenario: Compressed Claude Code session
- **WHEN** a Claude Code transcript exists only as `.jsonl.zst`
- **THEN** the session is listed as compressed with its title and activity counts

#### Scenario: Reading does not change the file
- **WHEN** a compressed session is indexed
- **THEN** the compressed file is unchanged

#### Scenario: Repeat listing is cheap
- **WHEN** a compressed session was indexed and has not changed
- **THEN** listing it again does not decompress it

### Requirement: Compression filter
The system SHALL offer a compression filter with the values uncompressed, compressed and both,
defaulting to uncompressed, in the browser and on the command line. It SHALL combine with every other
filter, including visibility.

#### Scenario: Default view
- **WHEN** the browser or `--list` runs with no compression filter chosen
- **THEN** compressed sessions are not shown

#### Scenario: Cycling in the browser
- **WHEN** the user presses `z`
- **THEN** the filter moves to the next value and the header shows it

#### Scenario: Command line
- **WHEN** `sessions --list --compression compressed` runs
- **THEN** only compressed sessions are listed

#### Scenario: Combined with hidden
- **WHEN** the visibility filter is hidden and the compression filter is compressed
- **THEN** only sessions that are both hidden and compressed are shown

### Requirement: Compressed rows are identifiable
The browser SHALL mark a compressed row as compressed and SHALL report how many sessions matching the
current filters and search are excluded only because they are compressed.

#### Scenario: Row marked
- **WHEN** a compressed session is shown
- **THEN** its status reads `compressed`

#### Scenario: Search that matches only compressed sessions
- **WHEN** a search matches compressed sessions that the filter excludes
- **THEN** the header states how many, and how to show them

### Requirement: Compressed is distinct from missing
The system SHALL report a transcript that exists only in compressed form as compressed, not as missing.

#### Scenario: Member of a linked conversation
- **WHEN** a linked conversation's member has been compressed
- **THEN** the member is reported as compressed

### Requirement: Compress and decompress on request
The system SHALL compress and decompress a selected session from the browser and from the command
line, including its subagent transcripts where the harness keeps them with it. It SHALL refuse a
session that is open or whose harness does not support compression.

#### Scenario: Compress from the browser
- **WHEN** the user presses `Z` on an uncompressed session that is not open
- **THEN** the session is compressed, stays listed under the compressed filter, and a message says so

#### Scenario: Decompress from the browser
- **WHEN** the user presses `Z` on a compressed session
- **THEN** the session is restored and is listed as uncompressed

#### Scenario: Linked conversation
- **WHEN** the user presses `Z` on a linked conversation
- **THEN** every member that is not open is compressed, or every member is restored if none is plain

#### Scenario: Open session refused
- **WHEN** the user tries to compress a session that is open
- **THEN** nothing changes and a message says it is open

#### Scenario: Command line
- **WHEN** `sessions --compress ID` or `sessions --decompress ID` runs
- **THEN** that session changes form and the command reports the result

#### Scenario: Failure
- **WHEN** compressing or decompressing fails
- **THEN** the error is shown and the session's content is unaffected

### Requirement: Opening a compressed session decompresses it first
The system SHALL ask before decompressing a compressed session that the user opens from the browser,
and SHALL then open it. Declining SHALL change nothing. Opening from the command line SHALL refuse with
instructions.

#### Scenario: User agrees
- **WHEN** the user presses Enter on a compressed session and confirms
- **THEN** the session is decompressed and opened

#### Scenario: User declines
- **WHEN** the user presses Enter on a compressed session and declines
- **THEN** the session stays compressed and does not open

#### Scenario: Command line resume
- **WHEN** `sessions --resume ID` names a compressed session
- **THEN** the command fails and says how to decompress it

### Requirement: Transpiling works only on uncompressed sessions
The system SHALL refuse to bridge or transpile from a compressed session, SHALL NOT offer other
harnesses as launch choices for it, and SHALL say how to decompress it.

#### Scenario: Launch choices
- **WHEN** a compressed session has no existing copy in another harness
- **THEN** its only launch choice is its own harness

#### Scenario: Bridge attempt
- **WHEN** a bridge is requested for a compressed session
- **THEN** it fails with a message saying the session is compressed and no copy is created

#### Scenario: Recorded copy compressed
- **WHEN** a session's recorded copy in the requested harness is compressed
- **THEN** the launch fails with a message and no duplicate copy is created

### Requirement: Supervision works only on uncompressed sessions
The system SHALL refuse to create a supervisor for a compressed session. A running supervisor whose
target becomes compressed SHALL be told so, and SHALL continue where it left off after decompression.
A compressed supervisor SHALL be decompressed when the user opens it with `S`.

#### Scenario: Create refused
- **WHEN** the user presses `S` on a compressed session with no supervisor
- **THEN** no supervisor is created and a message explains why

#### Scenario: Target compressed under a supervisor
- **WHEN** the target of a supervisor is compressed and the supervisor receives a prompt
- **THEN** the supervisor is told the target is compressed and its checkpoint is kept

### Requirement: Compressed transcripts are never written to
The system SHALL NOT append to or rewrite a compressed transcript.

#### Scenario: Rename
- **WHEN** a compressed session is renamed
- **THEN** the new name is kept in `sessions` and the compressed file is unchanged

### Requirement: Linked conversations survive compression
The system SHALL treat a compressed newest member of a linked conversation as compressed rather than
missing, and compressing and restoring a member SHALL leave the conversation and its checkpoints
valid. A compressed older or equivalent member SHALL NOT block the conversation.

#### Scenario: Head compressed
- **WHEN** the only newest member of a conversation is compressed
- **THEN** the conversation shows as compressed and opening it asks to decompress

#### Scenario: Older copy compressed
- **WHEN** an older member is compressed and a newer plain member exists
- **THEN** the conversation works normally from the plain member

#### Scenario: Round trip
- **WHEN** a linked member is compressed and restored
- **THEN** the conversation status is the same as before and the next launch reuses the same copy

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
