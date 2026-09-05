"""Local LiveAudio service client (liveaudio-service-client track).

OpenCohost supervises at most one headless ``liveaudio-service`` child process
as its STT backend: it spawns it on demand (loopback URIs only), discovers the
effective WS port from the service stdout ``ws_port`` event, and repoints the
:class:`~opencohost.api.ptt_session.PttController` runtime URI — never
persisting the ephemeral fallback port.

Manual/remote setups keep working untouched: a non-loopback ``stt_ws_uri``
(second capture PC) NEVER triggers a local spawn, and a manually launched
LiveAudio is discovered by a bounded ``base..base+9`` hello-handshake scan.

PRIVACY: service stdout events carry codes/ports/states/counters only. This
package never logs raw stdout lines, transcripts, audio, or secrets.
"""

from opencohost.stt.discovery import (
    EnsureOutcome,
    ensure_local_service,
    find_liveaudio_port,
    is_loopback_host,
    is_loopback_uri,
    validate_hello,
    with_port,
)
from opencohost.stt.supervisor import (
    DEFAULT_PORT_TIMEOUT,
    DEFAULT_READY_TIMEOUT,
    LiveAudioSupervisor,
    parse_service_line,
    resolve_installed_service_exe,
    resolve_liveaudio_command,
)

__all__ = [
    "DEFAULT_PORT_TIMEOUT",
    "DEFAULT_READY_TIMEOUT",
    "EnsureOutcome",
    "LiveAudioSupervisor",
    "ensure_local_service",
    "find_liveaudio_port",
    "is_loopback_host",
    "is_loopback_uri",
    "parse_service_line",
    "resolve_installed_service_exe",
    "resolve_liveaudio_command",
    "validate_hello",
    "with_port",
]
