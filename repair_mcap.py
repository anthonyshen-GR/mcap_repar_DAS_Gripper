#!/usr/bin/env python3
"""
repair_mcap.py — recovers DAS-Gripper mcap files affected by:

  1. "Schema corruption": a few extra trailing garbage bytes appended to an
     otherwise-valid protobuf FileDescriptorSet in a Schema record. Detected
     by attempting FileDescriptorSet.ParseFromString and, on failure, retrying
     with 1..MAX_TRIM trailing bytes stripped. No message data is touched —
     this is a lossless fix.

  2. "Truncation" / mid-file chunk corruption: one or more Chunk records whose
     compressed payload fails to decompress or fails its CRC check. These
     chunks are dropped (that slice of message data is genuinely unrecoverable);
     every other chunk is carried through unchanged.

Design note: this reads chunks by seeking directly to each chunk's offset as
recorded in the file's own Summary section (chunk_indexes), rather than
sequentially scanning the whole file byte-for-byte. That matters because a
corrupted chunk can desync a purely sequential parser for everything *after*
it, even when the rest of the file is perfectly fine — seeking chunk-by-chunk
avoids that.

Usage:
    python3 repair_mcap.py <input.mcap> <output.mcap>
"""
import sys
import json

from mcap.reader import make_reader
from mcap.stream_reader import breakup_chunk, CRCValidationError
from mcap.data_stream import ReadDataStream
from mcap.records import Schema, Channel, Message, Chunk
from mcap.writer import Writer

try:
    from google.protobuf import descriptor_pb2
    HAVE_PROTOBUF = True
except ImportError:
    HAVE_PROTOBUF = False

MAX_TRIM = 8  # max trailing bytes to try stripping from a schema payload


def try_fix_schema_bytes(schema: Schema):
    """Returns (possibly-fixed bytes, trimmed_count, was_broken)."""
    if schema.encoding != "protobuf" or not HAVE_PROTOBUF:
        return schema.data, 0, False

    fds = descriptor_pb2.FileDescriptorSet()
    try:
        fds.ParseFromString(schema.data)
        return schema.data, 0, False  # already fine
    except Exception:
        pass

    for trim in range(1, MAX_TRIM + 1):
        candidate = schema.data[: len(schema.data) - trim]
        try:
            fds.Clear()
            fds.ParseFromString(candidate)
            return candidate, trim, True
        except Exception:
            continue

    return schema.data, -1, True  # unrepairable, pass through untouched


def repair(input_path: str, output_path: str):
    report = {
        "input": input_path,
        "output": output_path,
        "schema_fixes": [],
        "schema_unrepairable": [],
        "dropped_chunks": [],
        "total_chunks": 0,
        "messages_written": 0,
    }

    with open(input_path, "rb") as f:
        reader = make_reader(f, validate_crcs=False)
        summary = reader.get_summary()
        if summary is None:
            raise RuntimeError(
                "No Summary section found - file's index/footer is missing or "
                "unreadable. This tool relies on the Summary to locate chunks; "
                "a file in this state needs full linear recovery instead."
            )

        writer = Writer(output_path)
        writer.start(profile="", library="genrobot-repair-tool")

        schema_id_map = {}
        channel_id_map = {}

        def ensure_schema(old_schema: Schema) -> int:
            if old_schema.id in schema_id_map:
                return schema_id_map[old_schema.id]
            fixed_data, trimmed, was_broken = try_fix_schema_bytes(old_schema)
            if was_broken:
                if trimmed >= 0:
                    report["schema_fixes"].append({
                        "schema_id": old_schema.id,
                        "name": old_schema.name,
                        "trimmed_bytes": trimmed,
                    })
                else:
                    report["schema_unrepairable"].append({
                        "schema_id": old_schema.id,
                        "name": old_schema.name,
                    })
            new_id = writer.register_schema(
                name=old_schema.name, encoding=old_schema.encoding, data=fixed_data
            )
            schema_id_map[old_schema.id] = new_id
            return new_id

        def ensure_channel(old_channel: Channel) -> int:
            if old_channel.id in channel_id_map:
                return channel_id_map[old_channel.id]
            new_schema_id = ensure_schema(summary.schemas[old_channel.schema_id])
            new_id = writer.register_channel(
                topic=old_channel.topic,
                message_encoding=old_channel.message_encoding,
                schema_id=new_schema_id,
                metadata=old_channel.metadata,
            )
            channel_id_map[old_channel.id] = new_id
            return new_id

        # Register every schema/channel up front from the trusted Summary index.
        for old_channel in summary.channels.values():
            ensure_channel(old_channel)

        chunk_indexes = sorted(summary.chunk_indexes, key=lambda c: c.chunk_start_offset)
        report["total_chunks"] = len(chunk_indexes)

        for ci in chunk_indexes:
            f.seek(ci.chunk_start_offset + 1 + 8)  # skip opcode(1) + record length(8)
            try:
                chunk = Chunk.read(ReadDataStream(f))
                chunk_records = breakup_chunk(chunk, validate_crc=True)
            except (CRCValidationError, Exception) as e:  # noqa: BLE001
                report["dropped_chunks"].append({
                    "message_start_time": ci.message_start_time,
                    "message_end_time": ci.message_end_time,
                    "chunk_start_offset": ci.chunk_start_offset,
                    "error": f"{type(e).__name__}: {e}",
                })
                continue

            for cr in chunk_records:
                if isinstance(cr, Message):
                    new_channel_id = ensure_channel(summary.channels[cr.channel_id])
                    writer.add_message(
                        channel_id=new_channel_id,
                        log_time=cr.log_time,
                        data=cr.data,
                        publish_time=cr.publish_time,
                        sequence=cr.sequence,
                    )
                    report["messages_written"] += 1
                # Schema/Channel records repeated inside chunks are already
                # covered by the up-front registration from the Summary above.

        writer.finish()

    report_path = output_path + ".report.json"
    with open(report_path, "w") as rf:
        json.dump(report, rf, indent=2)

    return report, report_path


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.mcap> <output.mcap>")
        sys.exit(1)
    report, report_path = repair(sys.argv[1], sys.argv[2])
    print("Repair complete.")
    print(f"  Chunks total:             {report['total_chunks']}")
    print(f"  Messages written:         {report['messages_written']}")
    print(f"  Schemas repaired:         {len(report['schema_fixes'])}")
    for s in report["schema_fixes"]:
        print(f"    - schema {s['schema_id']} ({s['name']}): trimmed {s['trimmed_bytes']} trailing byte(s)")
    print(f"  Schemas unrepairable:     {len(report['schema_unrepairable'])}")
    for s in report["schema_unrepairable"]:
        print(f"    - schema {s['schema_id']} ({s['name']}): could not be fixed, left as-is")
    print(f"  Chunks dropped (data loss): {len(report['dropped_chunks'])}")
    for c in report["dropped_chunks"]:
        dur_s = (c["message_end_time"] - c["message_start_time"]) / 1e9
        print(f"    - dropped ~{dur_s:.2f}s of data at offset {c['chunk_start_offset']} ({c['error']})")
    print(f"  Full report written to: {report_path}")


if __name__ == "__main__":
    main()
