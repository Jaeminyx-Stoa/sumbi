"""Keep paired hook observations locally, with salted IDs and session call ordering."""

from dataclasses import dataclass, field
from bisect import bisect_right
import hashlib
import hmac
import secrets

from sumbi.core.privacy import current_key
from sumbi.events.hook_denials import content_text, failed_envelope, recognize
from sumbi.events.schema import thaw


@dataclass
class HookCalls:
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    starts: dict = field(default_factory=dict)
    ends: dict = field(default_factory=dict)
    observations: list = field(default_factory=list)

    def endpoint(self, identity, when, order, *, start, error=None):
        if when is None:
            return
        target = self.starts if start else self.ends
        target.setdefault(identity, (when, (order, 0), error))

    def evidence(self, evidence, when, order, agent):
        if when is None:
            return
        for index, item in enumerate(evidence.inputs):
            if item.tool_call_id is not None:
                key = _identity(item)
                self.inputs.setdefault(key, (when, (order, index), item.name, item.tool_call_id))
        for index, item in enumerate(evidence.outputs):
            if item.tool_call_id is not None:
                text = content_text(thaw(item.output), agent)
                candidate = (text if text and text.startswith(("Command blocked by PreToolUse",
                    "PreToolUse", "Script error:", "{")) else None)
                self.outputs.setdefault(_identity(item),
                    (when, (order, index), item.error, candidate))

    def finish(self, agent, key):
        """Hash normalized text immediately; raw outputs leave this tracker afterward."""
        observed = set()
        for identity, (at, order, tool, call_id) in self.inputs.items():
            observed.add(call_id)
            output = self.outputs.get(identity)
            # Native item completions have error evidence without response text.
            if output is None and tool in ("CommandExecution", "FileChange"):
                end = self.ends.get(call_id)
                output = (*end, None) if end else None
            if output is None or (output[0], output[1]) < (at, order):
                result, denial = None, None
            else:
                ended, end_order, error, text = output
                denial = recognize(text, agent, tool) if error is not None else None
                success = (error is False and denial is None
                    and not failed_envelope(text, agent, tool))
                result = (ended, end_order, success)
            if denial is not None:
                category, normalized, timeout = denial
                digest = hmac.new(key, normalized.encode("utf-8", errors="surrogatepass"),
                    hashlib.sha256).hexdigest()[:24]
                denial = category, "reason_" + digest, timeout
            self.observations.append((at, order, tool, result, denial))
        for identity, (at, order, _) in self.starts.items():
            if identity not in observed:
                end = self.ends.get(identity)
                result = ((*end[:2], end[2] is False)
                    if end and end[:2] >= (at, order) else None)
                self.observations.append((at, order, None, result, None))
        self.inputs.clear()
        self.outputs.clear()
        self.starts.clear()
        self.ends.clear()

    def events(self, window):
        """Count result-time failures; recovery is bounded to observed calls before scan end."""
        calls = sorted(self.observations, key=lambda c: (c[0], c[1]))
        points = [(c[0], c[1]) for c in calls]
        successful = [result[:2] for _, _, _, result, _ in calls
            if result and result[2] and result[0] < window.until]
        failures = [(tool, result, denial) for _, _, tool, result, denial in calls
            if denial and result and window.contains(result[0])]
        first = min((result[:2] for _, _, _, result, denial in calls
            if denial and not denial[2] and result[0] < window.until), default=None)
        first_call = first is not None and not any(point < first for point in successful)
        for tool, result, (category, reason_id, timeout) in failures:
            index = bisect_right(points, result[:2])
            following = calls[index:index + 3]
            recovered = any(c[2] == tool and c[3] and c[3][2]
                and c[0] < window.until and c[3][0] < window.until for c in following)
            yield category, reason_id, timeout, recovered, first_call


def reason_key():
    """Owners supply a stable key; otherwise IDs are ephemeral within this build."""
    return current_key() or secrets.token_bytes(32)


def _identity(item):
    value = item.source_identity.value if item.source_identity is not None else item.tool_call_id
    return type(value), value
