"""Wire codec exports for the archive IPC boundary.

Frame mechanics live in :mod:`framing`; request, message, and response codecs
remain available from this historical package module.
"""

from .codecs import (
    ARCHIVE_ABORT_REQUEST_FIELDS,
    ARCHIVE_BEGIN_REQUEST_FIELDS,
    ARCHIVE_CHUNK_REQUEST_FIELDS,
    ARCHIVE_COMMIT_REQUEST_FIELDS,
    ARCHIVE_FAILURE_RESPONSE_FIELDS,
    ARCHIVE_MESSAGE_FIELDS,
    ARCHIVE_REQUEST_FIELDS,
    ARCHIVE_SUCCESS_RESPONSE_FIELDS,
    ArchiveWireCodec,
    archive_message_chunks,
    archive_message_from_wire,
    archive_message_to_wire,
    decode_archive_request,
    decode_archive_response,
    encode_archive_request,
    encode_archive_response,
    send_archive_response,
)
from .framing import (
    DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
    decode_archive_frame,
    encode_archive_frame,
    require_exact_mapping,
    validate_frame_limit,
)

_archive_message_chunks = archive_message_chunks
_archive_message_from_wire = archive_message_from_wire
_archive_message_to_wire = archive_message_to_wire
_decode_archive_request = decode_archive_request
_decode_archive_response = decode_archive_response
_encode_archive_request = encode_archive_request
_encode_archive_response = encode_archive_response
_require_exact_mapping = require_exact_mapping
_validate_frame_limit = validate_frame_limit

__all__ = [
    "ARCHIVE_ABORT_REQUEST_FIELDS",
    "ARCHIVE_BEGIN_REQUEST_FIELDS",
    "ARCHIVE_CHUNK_REQUEST_FIELDS",
    "ARCHIVE_COMMIT_REQUEST_FIELDS",
    "ARCHIVE_FAILURE_RESPONSE_FIELDS",
    "ARCHIVE_MESSAGE_FIELDS",
    "ARCHIVE_REQUEST_FIELDS",
    "ARCHIVE_SUCCESS_RESPONSE_FIELDS",
    "DEFAULT_MAX_ARCHIVE_FRAME_BYTES",
    "ArchiveWireCodec",
    "archive_message_chunks",
    "archive_message_from_wire",
    "archive_message_to_wire",
    "decode_archive_frame",
    "decode_archive_request",
    "decode_archive_response",
    "encode_archive_frame",
    "encode_archive_request",
    "encode_archive_response",
    "require_exact_mapping",
    "send_archive_response",
    "validate_frame_limit",
]
