# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula DataStream production adapters (PR 5).

Contains the deterministic wire codec and the Redpanda/Kafka transport
adapter. Provider types stay inside this package: nothing here leaks into
application or domain code.
"""

from darkula.infrastructure.data_stream.codec import (
    ENVELOPE_VERSION,
    MessageCodecError,
    decode_stream_message,
    encode_stream_message,
)
from darkula.infrastructure.data_stream.redpanda import RedpandaDataStream

__all__ = [
    "ENVELOPE_VERSION",
    "MessageCodecError",
    "RedpandaDataStream",
    "decode_stream_message",
    "encode_stream_message",
]
