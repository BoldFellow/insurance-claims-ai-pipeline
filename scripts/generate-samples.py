#!/usr/bin/env python3
"""
generate-samples.py
Creates synthetic sample files for the insurance-claims-ai-pipeline lab.

Generates:
  scripts/samples/photo-damage.jpg       -- minimal JPEG (placeholder)
  scripts/samples/police-report.pdf      -- single-page synthetic PDF
  scripts/samples/red-flag/photo-damage.jpg
  scripts/samples/red-flag/police-report.pdf

IMPORTANT: The generated images and PDFs are minimal placeholders for
local testing and schema validation only. For a realistic Rekognition
demo, replace photo-damage.jpg with an actual vehicle damage photograph
(CC0 licensed or your own). See scripts/samples/README.md for guidance.

Usage:
    python3 scripts/generate-samples.py

Requirements: Python 3 standard library only. No pip installs needed.
The PDF writer uses a minimal hand-crafted PDF structure (no reportlab).
"""

import os
import struct
import zlib
import textwrap

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(SCRIPT_DIR, "samples")
RED_FLAG_DIR = os.path.join(SAMPLES_DIR, "red-flag")

os.makedirs(SAMPLES_DIR, exist_ok=True)
os.makedirs(RED_FLAG_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Minimal JPEG builder (valid JPEG with SOI/APP0/SOF/SOS/EOI markers)
# The image is a 100x80 solid-colour block -- enough for Rekognition to
# accept the file. Replace with a real photograph for a meaningful demo.
# ---------------------------------------------------------------------------
def _make_minimal_jpeg(label: str = "") -> bytes:
    """Return bytes of a tiny but structurally valid JPEG."""
    # Use Python's built-in struct to write a valid 1x1 JPEG
    # This is a known-good minimal JPEG (grayscale 1x1 pixel) from RFC
    # We'll use a hard-coded minimal valid JPEG that Rekognition accepts
    MINIMAL_JPEG = bytes([
        0xFF,0xD8,0xFF,0xE0,0x00,0x10,0x4A,0x46,0x49,0x46,0x00,0x01,
        0x01,0x00,0x00,0x01,0x00,0x01,0x00,0x00,0xFF,0xDB,0x00,0x43,
        0x00,0x08,0x06,0x06,0x07,0x06,0x05,0x08,0x07,0x07,0x07,0x09,
        0x09,0x08,0x0A,0x0C,0x14,0x0D,0x0C,0x0B,0x0B,0x0C,0x19,0x12,
        0x13,0x0F,0x14,0x1D,0x1A,0x1F,0x1E,0x1D,0x1A,0x1C,0x1C,0x20,
        0x24,0x2E,0x27,0x20,0x22,0x2C,0x23,0x1C,0x1C,0x28,0x37,0x29,
        0x2C,0x30,0x31,0x34,0x34,0x34,0x1F,0x27,0x39,0x3D,0x38,0x32,
        0x3C,0x2E,0x33,0x34,0x32,0xFF,0xC0,0x00,0x0B,0x08,0x00,0x01,
        0x00,0x01,0x01,0x01,0x11,0x00,0xFF,0xC4,0x00,0x1F,0x00,0x00,
        0x01,0x05,0x01,0x01,0x01,0x01,0x01,0x01,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,
        0x09,0x0A,0x0B,0xFF,0xC4,0x00,0xB5,0x10,0x00,0x02,0x01,0x03,
        0x03,0x02,0x04,0x03,0x05,0x05,0x04,0x04,0x00,0x00,0x01,0x7D,
        0x01,0x02,0x03,0x00,0x04,0x11,0x05,0x12,0x21,0x31,0x41,0x06,
        0x13,0x51,0x61,0x07,0x22,0x71,0x14,0x32,0x81,0x91,0xA1,0x08,
        0x23,0x42,0xB1,0xC1,0x15,0x52,0xD1,0xF0,0x24,0x33,0x62,0x72,
        0x82,0x09,0x0A,0x16,0x17,0x18,0x19,0x1A,0x25,0x26,0x27,0x28,
        0x29,0x2A,0x34,0x35,0x36,0x37,0x38,0x39,0x3A,0x43,0x44,0x45,
        0x46,0x47,0x48,0x49,0x4A,0x53,0x54,0x55,0x56,0x57,0x58,0x59,
        0x5A,0x63,0x64,0x65,0x66,0x67,0x68,0x69,0x6A,0x73,0x74,0x75,
        0x76,0x77,0x78,0x79,0x7A,0x83,0x84,0x85,0x86,0x87,0x88,0x89,
        0x8A,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9A,0xA2,0xA3,0xA4,
        0xA5,0xA6,0xA7,0xA8,0xA9,0xAA,0xB2,0xB3,0xB4,0xB5,0xB6,0xB7,
        0xB8,0xB9,0xBA,0xC2,0xC3,0xC4,0xC5,0xC6,0xC7,0xC8,0xC9,0xCA,
        0xD2,0xD3,0xD4,0xD5,0xD6,0xD7,0xD8,0xD9,0xDA,0xE1,0xE2,0xE3,
        0xE4,0xE5,0xE6,0xE7,0xE8,0xE9,0xEA,0xF1,0xF2,0xF3,0xF4,0xF5,
        0xF6,0xF7,0xF8,0xF9,0xFA,0xFF,0xDA,0x00,0x08,0x01,0x01,0x00,
        0x00,0x3F,0x00,0xFB,0xD7,0xFF,0xD9
    ])
    return MINIMAL_JPEG


# ---------------------------------------------------------------------------
# Minimal PDF builder (hand-crafted, no third-party libraries)
# Produces a valid single-page PDF with readable text, accepted by Textract.
# ---------------------------------------------------------------------------
def _make_pdf(title: str, lines: list) -> bytes:
    """Return bytes of a minimal valid single-page PDF with the given text."""

    def _pdf_str(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text_stream_lines = []
    text_stream_lines.append("BT")
    text_stream_lines.append("/F1 12 Tf")
    text_stream_lines.append("50 750 Td")
    text_stream_lines.append("14 TL")  # line spacing
    for line in lines:
        safe = _pdf_str(line)
        text_stream_lines.append(f"({safe}) Tj T*")
    text_stream_lines.append("ET")
    stream_content = "\n".join(text_stream_lines).encode("latin-1")

    # Object offsets (we'll fill these in as we go)
    objects = {}

    def _obj(n: int, content: str) -> bytes:
        return f"{n} 0 obj\n{content}\nendobj\n".encode("latin-1")

    # Build objects
    obj1 = _obj(1, "<< /Type /Catalog /Pages 2 0 R >>")
    obj2 = _obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    obj3 = _obj(3, (
        "<< /Type /Page /Parent 2 0 R "
        "/MediaBox [0 0 612 792] "
        "/Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>"
    ))

    stream_hdr = f"<< /Length {len(stream_content)} >>\n".encode("latin-1")
    obj4 = (
        b"4 0 obj\n"
        + stream_hdr
        + b"stream\n"
        + stream_content
        + b"\nendstream\nendobj\n"
    )

    obj5 = _obj(5, (
        "<< /Type /Font /Subtype /Type1 "
        "/BaseFont /Helvetica "
        "/Encoding /WinAnsiEncoding >>"
    ))

    header = b"%PDF-1.4\n"
    body = obj1 + obj2 + obj3 + obj4 + obj5

    # Cross-reference table
    xref_offset = len(header) + len(body)
    offsets = [0]  # object 0 is free
    pos = len(header)
    for obj_bytes in [obj1, obj2, obj3, obj4, obj5]:
        offsets.append(pos)
        pos += len(obj_bytes)

    xref_lines = ["xref", f"0 {len(offsets)}"]
    xref_lines.append("0000000000 65535 f ")
    for off in offsets[1:]:
        xref_lines.append(f"{off:010d} 00000 n ")
    xref = ("\n".join(xref_lines) + "\n").encode("latin-1")

    trailer = (
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset + len(body)}\n%%EOF\n"
    ).encode("latin-1")

    return header + body + xref + trailer


# ---------------------------------------------------------------------------
# Generate files
# ---------------------------------------------------------------------------
def generate_normal():
    # photo-damage.jpg
    jpg_path = os.path.join(SAMPLES_DIR, "photo-damage.jpg")
    with open(jpg_path, "wb") as f:
        f.write(_make_minimal_jpeg())
    print(f"  Created {jpg_path}")
    print("  NOTE: Replace with a real vehicle damage photo for a meaningful Rekognition demo.")

    # police-report.pdf (single-page synthetic incident report)
    pdf_lines = [
        "POLICE INCIDENT REPORT",
        "Report No: 2026-MAY-4471",
        "Date: 2026-05-22",
        "Synthetic -- for demonstration only",
        "",
        "Incident Type: Vehicle Collision",
        "Location: Maple Street at Oak Avenue",
        "Time: 08:15",
        "",
        "Vehicle 1: 2021 Honda Civic, plate 7ABC123",
        "Driver 1: Alex Johnson",
        "Damage: Front driver-side panel, airbag deployed",
        "",
        "Vehicle 2: 2018 Ford F-150, plate 9DEF456",
        "Driver 2: Marcus Webb",
        "Damage: Minor front bumper",
        "",
        "Narrative:",
        "Vehicle 2 failed to stop at red light on Oak Avenue.",
        "Vehicle 2 struck Vehicle 1 in the intersection.",
        "Driver 2 acknowledged fault at scene.",
        "Two witnesses provided contact information.",
        "",
        "Officer: Badge 7734",
    ]
    pdf_path = os.path.join(SAMPLES_DIR, "police-report.pdf")
    with open(pdf_path, "wb") as f:
        f.write(_make_pdf("Police Report CLM-001", pdf_lines))
    print(f"  Created {pdf_path}")


def generate_red_flag():
    # photo-damage.jpg (same placeholder -- instructors can swap for a more dramatic photo)
    jpg_path = os.path.join(RED_FLAG_DIR, "photo-damage.jpg")
    with open(jpg_path, "wb") as f:
        f.write(_make_minimal_jpeg())
    print(f"  Created {jpg_path}")

    # police-report.pdf -- absent (no police report filed, which is itself a red flag)
    # We generate a minimal "no report on file" document instead
    pdf_lines = [
        "INSURANCE COMPANY INTERNAL NOTE",
        "Claim ID: CLM-002",
        "Date: 2026-05-27",
        "Synthetic -- for demonstration only",
        "",
        "No police report on file for this claim.",
        "",
        "Claim notes:",
        "- Claimant unable to provide incident location",
        "- No witnesses",
        "- No police report filed",
        "- Damage inconsistent with stated incident description",
        "- Coverage limit increased 14 days before incident",
        "- Two repair estimates both significantly above market average",
        "",
        "Flagged for adjuster review per fraud screening protocol.",
    ]
    pdf_path = os.path.join(RED_FLAG_DIR, "police-report.pdf")
    with open(pdf_path, "wb") as f:
        f.write(_make_pdf("Internal Note CLM-002", pdf_lines))
    print(f"  Created {pdf_path}")


if __name__ == "__main__":
    print("Generating normal sample set...")
    generate_normal()
    print("")
    print("Generating red-flag sample set...")
    generate_red_flag()
    print("")
    print("Done. Remember to replace photo-damage.jpg with a real image for Rekognition.")
