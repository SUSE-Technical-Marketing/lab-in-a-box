#!/usr/bin/env python3
"""Utilities for SOURCE_IMAGE and SOURCE_SHA256 handling.

SOURCE fields use a unified logic: they can be a URL, a local file path, or
(for SOURCE_SHA256) a checksum value. This module provides detection and
handling functions.
"""


def get_source_type(value):
    """Determine if value is URL, checksum, or local file path.

    Args:
        value: String to analyze (SOURCE_IMAGE or SOURCE_SHA256 value)

    Returns:
        str: One of "url", "checksum", "local_file", or None if empty
    """
    if not value:
        return None

    value = value.strip()

    # Check if it's a URL (http://, https://, ftp://, nfs://)
    protocols = ('http://', 'https://', 'ftp://', 'nfs://')
    if any(value.startswith(proto) for proto in protocols):
        return "url"

    # Check if it's a hex checksum (32/40/64/128 chars = MD5/SHA1/SHA256/SHA512)
    if len(value) in (32, 40, 64, 128):
        try:
            int(value, 16)  # Valid hex?
            return "checksum"
        except ValueError:
            pass

    # Otherwise it's a local file path
    return "local_file"


def is_url(value):
    """Check if value is a URL."""
    return get_source_type(value) == "url"


def is_checksum(value):
    """Check if value is a hex checksum."""
    return get_source_type(value) == "checksum"


def is_local_file(value):
    """Check if value is a local file path."""
    return get_source_type(value) == "local_file"
