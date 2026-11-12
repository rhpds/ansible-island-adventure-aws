#!/usr/bin/env python3
"""Extract the five student order fields from leaderboard ResourceClaim YAML.

Positional arguments are preserved. --format json outputs student variable names
(including openshift_cluster_common_password). Output contains credentials.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from provision_fields import REQUIRED_FIELDS, extract_provision_data, load_claim, student_fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", default=Path("provision-data.yaml"))
    parser.add_argument("output", type=Path, nargs="?", default=Path("provision-fields.txt"))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    try:
        claim = load_claim(args.input)
        fields = student_fields(claim)
        if args.format == "json":
            output = json.dumps(fields, indent=2)
        else:
            width = max(len(f["label"]) for f in REQUIRED_FIELDS)
            lines = [f"Provision fields for guid: {extract_provision_data(claim).get('guid', 'unknown')}", ""]
            lines.extend(f"  {f['label']:<{width}}  {fields[f['target']]}" for f in REQUIRED_FIELDS)
            output = "\n".join(lines)
        if args.output.is_symlink():
            raise ValueError("Output must not be a symbolic link")
        fd, temporary = tempfile.mkstemp(prefix=".provision-", dir=args.output.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(output + "\n")
            os.replace(temporary, args.output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(output)
        print(f"\nWritten to {args.output}", file=sys.stderr)
        return 0
    except (ValueError, OSError, TypeError, AttributeError) as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        print("ERROR: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
