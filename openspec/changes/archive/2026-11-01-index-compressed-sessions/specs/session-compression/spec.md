## ADDED Requirements

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
