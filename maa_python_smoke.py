import argparse
import json

from Arknights.addons.contrib.maa.maa_python import (
    MaaSession,
    describe_maa_python_runtime,
    ensure_maa_python_runtime,
    get_maa_paths,
    load_task_batch_from_config,
    run_task_batch,
)


def build_task_batch(args: argparse.Namespace) -> list[dict]:
    if args.mode == "common":
        return load_task_batch_from_config()
    if args.mode == "startup":
        return [
            {
                "type": "StartUp",
                "params": {
                    "client_type": args.client_type,
                    "start_game_enabled": True,
                },
            }
        ]
    if args.mode == "award":
        return [{"type": "Award", "params": {"award": True, "mail": True, "recruit": True}}]
    if args.mode == "fight":
        params = {
            "stage": args.stage,
            "times": args.times,
        }
        if args.series is not None:
            params["series"] = args.series
        if args.expiring_medicine is not None:
            params["expiring_medicine"] = args.expiring_medicine
        return [{"type": "Fight", "params": params}]
    raise ValueError(f"unsupported mode: {args.mode}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a minimal MAA Python smoke test")
    parser.add_argument(
        "--mode",
        choices=["paths", "prepare", "connect", "startup", "award", "fight", "common"],
        default="connect",
    )
    parser.add_argument("--client-type", default="Official")
    parser.add_argument("--stage", default="1-7")
    parser.add_argument("--times", type=int, default=1)
    parser.add_argument("--series", type=int, default=None)
    parser.add_argument("--expiring-medicine", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()

    if args.mode == "paths":
        print(json.dumps(describe_maa_python_runtime(get_maa_paths()), ensure_ascii=False, indent=2))
        return

    if args.mode == "prepare":
        paths = ensure_maa_python_runtime(force_download=args.force_download)
        print(json.dumps(describe_maa_python_runtime(paths), ensure_ascii=False, indent=2))
        return

    if args.mode == "connect":
        session = MaaSession()
        try:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "device": session.device,
                        "adb_path": session.adb_path,
                        "connection_config": session.connection_config,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            session.close()
        return

    result = run_task_batch(build_task_batch(args), timeout=args.timeout)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
