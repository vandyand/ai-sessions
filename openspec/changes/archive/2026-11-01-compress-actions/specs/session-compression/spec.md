## ADDED Requirements

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
