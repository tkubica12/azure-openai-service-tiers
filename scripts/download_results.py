"""Download result blobs (Entra ID auth, no storage keys) into ./results/blobs/ - incremental by default."""
from __future__ import annotations

import argparse

from azure.core.exceptions import HttpResponseError
from azure.storage.blob import ContainerClient

from common import RESULTS_DIR, credential, outputs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="", help="only blobs starting with this prefix (e.g. campaign name)")
    ap.add_argument("--full", action="store_true", help="re-download everything, even unchanged files")
    args = ap.parse_args()

    out = outputs()
    container = ContainerClient(out["storageAccountUrl"], "results", credential=credential())
    target = RESULTS_DIR / "blobs"
    count = skipped = 0
    try:
        for blob in container.list_blobs(name_starts_with=args.prefix or None):
            path = target / blob.name
            # incremental: result records are immutable; batch-job.json is a mutable state file
            if (not args.full and path.exists() and path.stat().st_size == blob.size
                    and not blob.name.endswith("batch-job.json")):
                skipped += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(container.download_blob(blob.name).readall())
            count += 1
    except HttpResponseError as e:
        if e.status_code == 403:
            raise SystemExit("403 from storage - your public IP is probably not in the storage firewall. "
                             "Run ./infra/allow-my-ip.ps1 and retry.") from e
        raise
    print(f"downloaded {count} blobs, skipped {skipped} unchanged, into {target}")


if __name__ == "__main__":
    main()
