"""
stdfcompanion.writer
~~~~~~~~~~~~~~~~~~~~

Low-level STDF binary writer.  Knows how to serialize pystdf record types
back to the on-disk STDF v4 binary format.

STDF record layout
------------------
Every record starts with a 4-byte header:
    REC_LEN  U*2   number of bytes that follow the header
    REC_TYP  U*1   record type code
    REC_SUB  U*1   record sub-type code

Followed by REC_LEN bytes of field data whose byte order matches the
CPU_TYPE declared in the FAR record at the top of the file.

This module always writes little-endian (CPU_TYPE = 2) which is the
standard for Intel/x86 systems and the most common format in practice.
"""

from __future__ import annotations

import struct
import io
from typing import Any, BinaryIO, List, Optional, Sequence

from pystdf import V4

# Endianness used for all output files – little-endian (Intel / CPU_TYPE=2)
_ENDIAN = "<"

# --- pack-format map matching pystdf Types.packFormatMap ---
_PACK_FMT: dict[str, str] = {
    "C1": "c",
    "B1": "B",
    "U1": "B",
    "U2": "H",
    "U4": "I",
    "U8": "Q",
    "I1": "b",
    "I2": "h",
    "I4": "i",
    "I8": "q",
    "R4": "f",
    "R8": "d",
}


def _pack_scalar(fmt: str, value: Any) -> bytes:
    """Pack a single scalar value using the STDF format code."""
    if fmt == "C1":
        # character – make sure it is a single bytes object
        if isinstance(value, str):
            value = value.encode("ascii", errors="replace")
        if isinstance(value, (bytes, bytearray)):
            value = value[:1].ljust(1, b" ")
        return struct.pack(_ENDIAN + "c", value)
    return struct.pack(_ENDIAN + _PACK_FMT[fmt], value if value is not None else 0)


def _pack_cn(value: Optional[str]) -> bytes:
    """Pack a variable-length string (C*n): 1-byte length prefix + bytes."""
    if value is None:
        return b"\x00"
    encoded = value.encode("ascii", errors="replace")
    length = min(len(encoded), 255)
    return struct.pack("B", length) + encoded[:length]


def _pack_bn(value: Optional[list]) -> bytes:
    """Pack a variable-length byte array (B*n): 1-byte length prefix + bytes."""
    if not value:
        return b"\x00"
    length = min(len(value), 255)
    data = bytes([v & 0xFF for v in value[:length]])
    return struct.pack("B", length) + data


def _pack_dn(value: Optional[list]) -> bytes:
    """Pack a variable-length bit array (D*n): 2-byte bit-count prefix + bytes."""
    if not value:
        return b"\x00\x00"
    # value is a list of bytes; count bits
    nbits = len(value) * 8
    nbits = min(nbits, 65535)
    return struct.pack(_ENDIAN + "H", nbits) + bytes(value)


def _pack_array(fmt: str, values: Optional[list]) -> bytes:
    """Pack a kxTYPE array field."""
    if not values:
        return b""
    buf = io.BytesIO()
    elem_fmt = fmt  # e.g. "U2", "U1", "Cn", …
    if elem_fmt == "Cn":
        for v in values:
            buf.write(_pack_cn(v))
    elif elem_fmt == "N1":
        # nibbles – pack two per byte
        for i in range(0, len(values), 2):
            lo = values[i] & 0x0F
            hi = (values[i + 1] & 0x0F) if i + 1 < len(values) else 0
            buf.write(struct.pack("B", lo | (hi << 4)))
    else:
        for v in values:
            buf.write(struct.pack(_ENDIAN + _PACK_FMT[elem_fmt], v if v is not None else 0))
    return buf.getvalue()


def _serialize_fields(rec_type, fields: List[Any]) -> bytes:
    """Serialize the field list of a record to raw bytes (no header)."""
    buf = io.BytesIO()
    stdf_types = rec_type.fieldStdfTypes

    for i, stdf_type in enumerate(stdf_types):
        value = fields[i] if i < len(fields) else None

        if stdf_type.startswith("k"):
            # kNFMT or k0FMT – array field
            import re
            m = re.match(r"k(\d+)([A-Za-z][a-z0-9]*)", stdf_type)
            if m:
                elem_fmt = m.group(2)
                buf.write(_pack_array(elem_fmt, value))
        elif stdf_type == "Cn":
            buf.write(_pack_cn(value))
        elif stdf_type == "Bn":
            buf.write(_pack_bn(value))
        elif stdf_type == "Dn":
            buf.write(_pack_dn(value))
        elif stdf_type == "Vn":
            # Generic Data Record – pass through raw bytes if already packed,
            # otherwise skip (GDR is rarely encountered in simple merges)
            pass
        else:
            # scalar
            if value is None:
                # write the "missing" zero bytes for the field size
                size = struct.calcsize(_ENDIAN + _PACK_FMT.get(stdf_type, "B"))
                buf.write(b"\x00" * size)
            else:
                buf.write(_pack_scalar(stdf_type, value))

    return buf.getvalue()


def pack_record(rec_type, fields: List[Any]) -> bytes:
    """
    Serialize a pystdf record (type + field list) to a complete STDF binary
    record including the 4-byte header.

    Parameters
    ----------
    rec_type : pystdf V4 record class (e.g. V4.Far, V4.Mir, …)
    fields   : list of field values as produced by pystdf's Parser

    Returns
    -------
    bytes
        Complete STDF binary record ready to be written to file.
    """
    body = _serialize_fields(rec_type, fields)
    rec_len = len(body)
    header = struct.pack(_ENDIAN + "HBB", rec_len, rec_type.typ, rec_type.sub)
    return header + body


class StdfWriter:
    """
    Writes pystdf records to a binary STDF file.

    Usage::

        with StdfWriter("output.stdf") as writer:
            writer.write_record(V4.Far, [2, 4])   # CPU_TYPE=2, STDF_VER=4
            writer.write_record(V4.Mir, mir_fields)
            ...

    Or pass a file-like object::

        with open("output.stdf", "wb") as f:
            writer = StdfWriter(f)
            writer.write_record(...)
    """

    def __init__(self, dest):
        """
        Parameters
        ----------
        dest : str or path-like or binary file object
            Destination file.  A string/path is opened; otherwise used as-is.
        """
        if hasattr(dest, "write"):
            self._file = dest
            self._owns_file = False
        else:
            self._file = open(dest, "wb")
            self._owns_file = True

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        if self._owns_file and self._file and not self._file.closed:
            self._file.close()

    # ------------------------------------------------------------------
    # Writing helpers
    # ------------------------------------------------------------------

    def write_record(self, rec_type, fields: List[Any]) -> int:
        """
        Write a single record to the output file.

        Parameters
        ----------
        rec_type : pystdf V4 record class
        fields   : list of field values

        Returns
        -------
        int
            Number of bytes written (including the 4-byte header).
        """
        data = pack_record(rec_type, fields)
        self._file.write(data)
        return len(data)

    def write_raw(self, data: bytes) -> int:
        """Write pre-packed bytes directly (e.g. pass-through of unknown records)."""
        self._file.write(data)
        return len(data)
