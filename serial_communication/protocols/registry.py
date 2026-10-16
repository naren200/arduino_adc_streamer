"""Protocol algorithms are Python; board parameters and adapter selection are JSON."""
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class ProtocolAdapter:
    id: str
    implementation: str | None
    context: object = None

    def configure(self, service, request):
        if self.implementation is None:
            from config.profile_configuration import configure
            return configure(service, request)
        return getattr(service, self.implementation)(request)

    def complete(self, service, request, result):
        if self.context.mode.definition.get('configuration', {}).get('status'):
            from config.profile_configuration import complete
            complete(service, request, result)

    def verify(self, service, request, status, device_mode):
        if self.context.mode.definition.get('configuration'):
            from config.profile_configuration import verify
            return verify(request, status)
        return service._verify_legacy_configuration(request, status, device_mode)

    def stopped_status(self, session):
        return session.read_profile_status() if self.context.mode.definition.get('configuration', {}).get('status') else None

    def capture_engine(self, status):
        field = self.context.mode.definition.get('configuration', {}).get('capture_engine_field')
        if field:
            from config.profile_configuration import status_value
            return status_value(status, field) if status else None
        return None


PROTOCOL_ADAPTERS = {
    name: ProtocolAdapter(name, '_send_legacy_config')
    for name in ('legacy_adc', 'legacy_555', 'array_dual')
}
PROTOCOL_ADAPTERS['profile_commands'] = ProtocolAdapter('profile_commands', None)


def protocol_adapter(context):
    from dataclasses import replace
    return replace(PROTOCOL_ADAPTERS[context.mode.definition['adapters']['protocol']], context=context)


# These are established codec families, not board identity tests. The stream
# reader owns shared framing; downstream processors own mode-specific payloads.
@dataclass(frozen=True)
class TimedU16Codec:
    id: str
    sample_bytes: int = 2

    def decode(self, payload):
        return np.frombuffer(payload, dtype='<u2')


FRAME_ADAPTERS = {name: TimedU16Codec(name) for name in
                  ('adc_u16_timed', 'ads7953_u16_timed', 'timer_555', 'pzt_rs_u16')}
