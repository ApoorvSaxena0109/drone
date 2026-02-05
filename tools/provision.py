"""Standalone provisioning script.

Can be run directly on a Jetson device:
    python tools/provision.py --org-id zypher-prototype

Creates the identity directory, generates keys, and outputs
the operator credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.security.identity import DroneIdentity


def main():
    parser = argparse.ArgumentParser(description="Provision a new drone identity")
    parser.add_argument(
        "--org-id",
        default="zypher-prototype",
        help="Organization ID (default: zypher-prototype)",
    )
    parser.add_argument(
        "--identity-dir",
        default="/etc/drone/identity",
        help="Identity directory (default: /etc/drone/identity)",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Write provisioning result to JSON file",
    )
    args = parser.parse_args()

    identity = DroneIdentity(identity_dir=args.identity_dir)

    if identity.is_provisioned:
        print(f"ERROR: Drone already provisioned ({identity.drone_id})")
        print(f"To re-provision, delete {args.identity_dir} and try again.")
        sys.exit(1)

    print("Provisioning drone identity...")
    print(f"  Organization: {args.org_id}")
    print(f"  Identity dir: {args.identity_dir}")
    print("")

    result = identity.provision(org_id=args.org_id)

    print("=== PROVISIONING COMPLETE ===")
    print(f"  Drone ID:           {result['drone_id']}")
    print(f"  Org ID:             {result['org_id']}")
    print(f"  Hardware Finger:    {result['hardware_fingerprint'][:16]}...")
    print(f"  Operator ID:        {result['operator_id']}")
    print(f"  Operator API Key:   {result['operator_api_key']}")
    print("")
    print("IMPORTANT: Save the API key. It will not be shown again.")

    if args.output_json:
        # Don't include the private key or raw api_key in persistent output
        safe_result = {
            "drone_id": result["drone_id"],
            "org_id": result["org_id"],
            "hardware_fingerprint": result["hardware_fingerprint"],
            "operator_id": result["operator_id"],
        }
        with open(args.output_json, "w") as f:
            json.dump(safe_result, f, indent=2)
        print(f"Provisioning info saved to: {args.output_json}")


if __name__ == "__main__":
    main()
