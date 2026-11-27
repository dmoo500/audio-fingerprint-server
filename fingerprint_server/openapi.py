"""Describe the existing HTTP wire contract without changing runtime validation."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Literal

from audio_fingerprint.codec import ENCODING_ID, MAX_ENCODED_BYTES
from pydantic import BaseModel, Field

from fingerprint_server.schemas import (
    AvailabilityRequest,
    AvailabilityResponse,
    LandmarkQuery,
    Submission,
)

State = Literal["queued", "processing", "processed", "failed", "pending_metadata"]


class Health(BaseModel):
    status: Literal["ok", "failed"]


class SubmissionReceipt(BaseModel):
    submission_id: str
    status: Literal["queued", "pending_metadata", "processed"]


class SubmissionStatus(BaseModel):
    id: str
    state: State
    attempts: int
    error: str | None
    reference_id: str | None


class Capabilities(BaseModel):
    api_version: Literal[1]
    fingerprint_formats: list[str]
    fingerprint_encodings: list[str]
    max_document_bytes: int
    availability_version: Literal[1] | None = None
    release_hints_version: Literal[1] | None = None
    recognition_landmarks_version: Literal[1] | None = None


class ReferenceCounts(BaseModel):
    total: int = Field(description="Searchable acoustic references, including retained variants.")
    partial: int = Field(description="Searchable references with incomplete acoustic coverage.")
    full: int = Field(description="Searchable references with full acoustic coverage.")
    excluded: int = Field(description="Retained references withheld from recognition.")


class RecordingCounts(BaseModel):
    total: int = Field(description="Searchable logical recordings.")
    partial: int = Field(
        description="Recordings with searchable variants but no full-coverage variant."
    )
    full: int = Field(description="Recordings with at least one full-coverage searchable variant.")
    excluded: int = Field(description="Logical recordings with no searchable variants.")


class SubmissionCounts(BaseModel):
    queued: int
    processing: int
    processed: int
    failed: int
    pending_metadata: int


class Statistics(BaseModel):
    references: ReferenceCounts
    recordings: RecordingCounts
    submissions: SubmissionCounts
    worker_available: bool


class Match(BaseModel):
    reference_id: str
    matched_landmarks: int
    query_landmarks: int
    match_fraction: float
    offset_seconds: float
    matched_span_seconds: float
    metadata: dict[str, Any]


class RecognitionCandidate(BaseModel):
    metadata: dict[str, Any]
    match_fraction: float = Field(ge=0, le=1)
    reference_id: str


class Recognition(BaseModel):
    status: Literal["matched", "no_match", "ambiguous"]
    match: Match | None
    reason: str = Field(default="", description="Only present for some no_match responses.")
    candidates: list[RecognitionCandidate] = Field(
        default_factory=list,
        max_length=2,
        description="Only returned for ambiguous PCM requests with include_candidates=true.",
    )


class Error(BaseModel):
    detail: str


class Artwork(BaseModel):
    status: Literal["pending", "ready", "missing", "unavailable", "busy"]
    url: str | None


def build_schema(gateway: bool = True) -> dict[str, Any]:
    """Generate a deployment-specific schema without starting the app or reading secrets."""
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "Audio Fingerprint HTTP API",
            "version": "1.0.0",
            "description": (
                "Experimental independent fingerprint service. "
                "No official Music Assistant affiliation. "
                "Gateway mode requires BOTH personal and application credentials. "
                "Gateway errors may use a different body than backend errors. "
                "Recognition PCM is transient; no downloading or audio export is provided."
                if gateway
                else "Legacy direct-backend mode: use the upload or read key for each operation."
            ),
        },
        "paths": {},
        "components": {"schemas": {}},
    }
    schemas = schema["components"]["schemas"]
    schemas["ValidationError"] = {
        "title": "ValidationError",
        "type": "object",
        "required": ["loc", "msg", "type"],
        "properties": {
            "loc": {
                "title": "Location",
                "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "msg": {"title": "Message", "type": "string"},
            "type": {"title": "Error Type", "type": "string"},
            "input": {"title": "Input"},
            "ctx": {"title": "Context", "type": "object"},
        },
    }
    schemas["HTTPValidationError"] = {
        "title": "HTTPValidationError",
        "type": "object",
        "properties": {
            "detail": {
                "title": "Detail",
                "type": "array",
                "items": {"$ref": "#/components/schemas/ValidationError"},
            }
        },
    }

    def register(model: type[BaseModel]) -> dict[str, str]:
        definition = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(definition.pop("$defs", {}))
        schemas[model.__name__] = definition
        return {"$ref": f"#/components/schemas/{model.__name__}"}

    for schema_model in (
        Health,
        SubmissionReceipt,
        SubmissionStatus,
        Capabilities,
        Statistics,
        Recognition,
        Error,
        Submission,
        AvailabilityRequest,
        AvailabilityResponse,
        LandmarkQuery,
    ):
        register(schema_model)
    compact = copy.deepcopy(schemas["Submission"])
    compact["title"] = "CompactSubmission"
    compact["properties"]["fingerprints"] = {
        "type": "string",
        "format": "byte",
        "maxLength": MAX_ENCODED_BYTES,
        "description": "Base64 sha1-80-offset-delta-v1 payload; decoded limits also apply.",
    }
    compact["properties"]["fingerprints_encoding"] = {"type": "string", "const": ENCODING_ID}
    compact["required"].append("fingerprints_encoding")
    schemas["CompactSubmission"] = compact
    security = {
        "ScopeKey": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Upload or read key, according to the operation.",
        }
    }
    if gateway:
        security = {
            "PersonalKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            "ApplicationKey": {"type": "apiKey", "in": "header", "name": "X-Application-Key"},
        }
    schema["components"]["securitySchemes"] = security
    operations: dict[str, tuple[str, type[BaseModel], str, str | None, str]] = {
        "/health": ("get", Health, "health", None, "Health"),
        "/api/v1/dejavu/statistics": ("get", Statistics, "getStatistics", "read", "Statistics"),
        "/api/v1/dejavu/artwork/{reference}": ("get", Artwork, "getArtwork", "read", "Artwork"),
        "/api/v1/dejavu/capabilities": (
            "get",
            Capabilities,
            "getCapabilities",
            "upload",
            "Capabilities",
        ),
        "/api/v1/dejavu/availability": (
            "post",
            AvailabilityResponse,
            "getAvailability",
            "upload",
            "Availability",
        ),
        "/api/v1/dejavu/submissions": (
            "post",
            SubmissionReceipt,
            "submitFingerprints",
            "upload",
            "Submit",
        ),
        "/api/v1/dejavu/submissions/{submission_id}": (
            "get",
            SubmissionStatus,
            "getSubmission",
            "upload",
            "Submission Status",
        ),
        "/api/v1/dejavu/recognize": ("post", Recognition, "recognizePcm", "read", "Recognize"),
        "/api/v1/dejavu/recognize/landmarks": (
            "post",
            Recognition,
            "recognizeLandmarks",
            "read",
            "Recognize Landmarks",
        ),
    }
    for path, (method, model, identifier, scope, summary) in operations.items():
        operation: dict[str, Any] = {"summary": summary}
        schema["paths"][path] = {method: operation}
        if "{" in path:
            name = path.split("{", 1)[1].removesuffix("}")
            operation["parameters"] = [
                {
                    "in": "path",
                    "name": name,
                    "required": True,
                    "schema": {"type": "string", "title": name.replace("_", " ").title()},
                }
            ]
        operation["operationId"] = identifier
        operation["security"] = [{name: [] for name in security}] if scope else []
        operation["tags"] = [scope or "health"]
        operation["description"] = (
            f"Requires {scope} access. " if scope else "Public backend health check. "
        )
        response = {
            "description": "Success",
            "content": {"application/json": {"schema": register(model)}},
        }
        operation["responses"] = {"200": response}
        codes = [401, 403, 429] if scope else []
        if identifier == "health":
            operation["responses"]["503"] = {**response, "description": "Worker unavailable"}
        else:
            codes.append(503)
        if identifier == "getSubmission":
            codes.append(404)
        if identifier == "getArtwork":
            codes.append(422)
            operation["description"] += (
                "Queue or read cached album artwork for a published reference. "
                "The worker resolves the album through MusicBrainz and Cover Art Archive. "
                "Poll pending results no more than once every three seconds. "
                "Missing artwork and transient unavailability do not affect recognition."
            )
        if identifier == "getAvailability":
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": register(AvailabilityRequest)}},
            }
            operation["description"] += "At most 100 identities and 128,000 request bytes."
        if identifier == "recognizeLandmarks":
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": register(LandmarkQuery)}},
            }
            operation["description"] += (
                "Transient precomputed landmarks for a 3–15 second capture normalized to "
                "44,100 Hz mono. Offsets are relative to the capture, in 2,048-sample hops. "
                "At most 10,000 distinct pairs and 512,000 request bytes. "
                "Shares the PCM endpoint's backend request budget, concurrency slots and "
                "acceptance rules. Does not store or publish submitted landmarks."
            )
        if method == "post":
            codes.extend([408, 413, 415, 422])
        if identifier == "submitFingerprints":
            codes.append(409)
            operation["responses"]["202"] = {**response, "description": "Durably accepted"}
            operation["parameters"] = [
                {
                    "in": "header",
                    "name": "Idempotency-Key",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "description": "SHA-256 of decoded canonical JSON: sorted keys, "
                    "compact separators, ASCII escaping, no fingerprints_encoding. "
                    "Not the upload bytes hash.",
                }
            ]
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                {"$ref": "#/components/schemas/Submission"},
                                {"$ref": "#/components/schemas/CompactSubmission"},
                            ]
                        }
                    }
                },
            }
            operation["description"] += (
                "Max 36,000,000 bytes uploaded AND decoded. v2: 1,000,000 landmarks, "
                "30 minutes full coverage. v1: 2,000,000 bytes, 100,000 landmarks, 3 windows. "
                "202 is not publication; 200 means an already processed duplicate."
            )
        if identifier == "recognizePcm":
            operation["parameters"] = [
                {
                    "in": "query",
                    "name": "sample_rate",
                    "required": True,
                    "schema": {
                        "type": "integer",
                        "minimum": 8000,
                        "maximum": 192000,
                        "title": "Sample Rate",
                    },
                },
                {
                    "in": "query",
                    "name": "channels",
                    "required": False,
                    "schema": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2,
                        "default": 1,
                        "title": "Channels",
                    },
                },
                {
                    "in": "query",
                    "name": "include_candidates",
                    "required": False,
                    "schema": {"type": "boolean", "default": False},
                    "description": (
                        "Include individually qualified candidates for ambiguous results."
                    ),
                },
            ]
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
            operation["description"] += (
                "Raw interleaved little-endian float32 PCM, normalized finite samples in [-1,1], "
                "3-15 seconds, 8000-192000 Hz, 1-2 channels. No WAV/container or multipart data. "
                "Byte limit is 15 * sample_rate * channels * 4. match is null unless matched. "
                "Candidate fractions are not probabilities."
            )
        for code in codes:
            operation["responses"][str(code)] = {
                "description": f"HTTP {code}; gateway errors may have a different body.",
                "content": {"application/json": {"schema": register(Error)}},
            }
            if code in (429, 503):
                operation["responses"][str(code)]["headers"] = {
                    "Retry-After": {
                        "description": "Seconds until retry, when provided.",
                        "schema": {"type": "string"},
                    }
                }
        if identifier == "recognizePcm":
            # Query validation has a structured error body, unlike manual document validation.
            operation["responses"]["422"]["content"]["application/json"]["schema"] = {
                "oneOf": [register(Error), {"$ref": "#/components/schemas/HTTPValidationError"}]
            }
    return schema


def main() -> None:
    """Export a deterministic OpenAPI artifact without opening storage or making requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("openapi.json"))
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(build_schema(gateway=not args.legacy), indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
