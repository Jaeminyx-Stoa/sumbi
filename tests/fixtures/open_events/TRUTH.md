# Synthetic open-events flow truth

Every record is authored machine evidence. `/fixture/workspace` is replaced with a
temporary test directory before collection; no recorded session data is used.

The valid stream has one coordinator and one worker from distinct emitters.
The coordinator reports 40 tokens. The worker reports 130 tokens, including two
reasoning tokens that are an output subset, and one tool interval of one second.
The worker edits at second two, starts the declared verifier at second three,
finishes with exit zero at second four, and ends at second six. Its local state
is success and its observed lifetime is six seconds. Its global parent ID links
to the coordinator even though their emitter identities differ.

The resumed file replays two event IDs exactly. Reading both files preserves
170 observed tokens and reports two duplicate events. Malformed versions and
conflicting IDs are appended only inside temporary test trees.
