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


def download_and_validate_source(source_url, dest_path, checksum_value=None, checksum_url=None):
    """Download source image from URL and optionally validate checksum.

    Args:
        source_url: URL to download from (http://, https://, ftp://, nfs://)
        dest_path: Local path where to save the downloaded file
        checksum_value: Optional inline checksum to validate against
        checksum_url: Optional URL to fetch checksum from

    Returns:
        bool: True if download successful (and validated if checksum provided)

    Raises:
        Exception: If download fails or checksum validation fails
    """
    import subprocess
    import hashlib
    from pathlib import Path

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Download the file
    try:
        if source_url.startswith(('http://', 'https://', 'ftp://')):
            # Use curl/wget for HTTP(S)/FTP
            result = subprocess.run(
                ['curl', '-f', '-L', '-o', str(dest), source_url],
                check=True,
                capture_output=True,
                text=True,
                timeout=3600
            )
        elif source_url.startswith('nfs://'):
            # NFS mount and copy
            # This is complex; for now raise not implemented
            raise NotImplementedError(f"NFS URL support not yet implemented: {source_url}")
        else:
            raise ValueError(f"Unsupported URL protocol: {source_url}")
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to download {source_url}: {e.stderr}")

    # Validate checksum if provided
    if checksum_value or checksum_url:
        # Get the checksum to validate against
        if checksum_url:
            # Fetch checksum from URL
            if get_source_type(checksum_url) == "url":
                try:
                    result = subprocess.run(
                        ['curl', '-f', '-s', checksum_url],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=300
                    )
                    # Parse checksum file (sha256sum format: "hash filename")
                    checksum_line = result.stdout.strip().split('\n')[0]
                    checksum_value = checksum_line.split()[0] if checksum_line else None
                except Exception as e:
                    raise Exception(f"Failed to fetch checksum from {checksum_url}: {e}")
            else:
                # Read from local file
                try:
                    with open(checksum_url, 'r') as f:
                        checksum_line = f.readline().strip()
                        checksum_value = checksum_line.split()[0] if checksum_line else None
                except Exception as e:
                    raise Exception(f"Failed to read checksum from {checksum_url}: {e}")

        # Calculate actual checksum
        if checksum_value:
            sha256_hash = hashlib.sha256()
            with open(dest, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256_hash.update(chunk)
            actual_checksum = sha256_hash.hexdigest()

            if actual_checksum.lower() != checksum_value.lower():
                dest.unlink()  # Remove the bad file
                raise Exception(f"Checksum mismatch for {source_url}: expected {checksum_value}, got {actual_checksum}")

    return True
