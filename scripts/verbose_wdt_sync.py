"""One-off ops tool: verbose WDT sync runner.

Identical wiring to ``cli live-sync --source wdt``, but wraps the read
gateway so every dataset window logs READ/OK/FAIL lines (including the
real exception on failure) to the container log.  Used to diagnose
``source_read_failed`` runs whose safe CLI output hides the root cause.
Nothing here prints credentials: exception text carries gateway status
codes / dataset names only.
"""

import sys
from pathlib import Path

sys.path.insert(0, "/app")  # 脚本挂载在 /tmp，代码库在镜像 /app

from common.public_data.cli import (
    build_service,
    load_settings,
    load_source_credentials,
    require_live_run,
)
from common.public_data.manifest import load_manifest


class VerboseGateway:
    def __init__(self, inner):
        self._inner = inner

    def read_dataset(self, dataset):
        print(f"READ {dataset.dataset} {dataset.window_start}..{dataset.window_end}", flush=True)
        try:
            records = self._inner.read_dataset(dataset)
        except Exception as exc:
            print(
                f"FAIL {dataset.dataset} {type(exc).__name__}: {str(exc)[:300]}",
                flush=True,
            )
            raise
        print(f" OK  {dataset.dataset} records={len(records)}", flush=True)
        return records


def main():
    settings = load_settings()
    require_live_run(settings, live_read=True, confirm_local_test_write=True)
    manifest = load_manifest(Path("/run/live-input/manifest.json"))
    credentials = load_source_credentials(
        "/run/live-input/source-credentials.json", source="wdt"
    )
    service = build_service(settings, credentials, manifest)
    service._wdt_gateway = VerboseGateway(service._wdt_gateway)
    result = service.sync(manifest, source="wdt")
    print(
        f"SYNC COMPLETED run_id={result.run_id} datasets={len(result.datasets)}",
        flush=True,
    )


if __name__ == "__main__":
    sys.exit(main())
