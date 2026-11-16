"""Run with ``python -m krakendf`` or the installed ``krakendf`` command."""
import argparse
import os


def build_parser():
    parser = argparse.ArgumentParser(description="KrakenDF local receiver and web interface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--data-dir", help="Persistent configuration, recordings and map cache")
    parser.add_argument("--source", choices=("live", "simulation", "idle"), default=os.environ.get("KRAKENDF_SOURCE", "live"))
    parser.add_argument("--daq-mode", choices=("managed", "external"), default=os.environ.get("KRAKENDF_DAQ_MODE", "managed"),
                        help="managed starts the bundled Linux USB stack; external connects to an existing receiver")
    parser.add_argument("--daq-host", default=os.environ.get("KRAKENDF_DAQ_HOST", "127.0.0.1"))
    parser.add_argument("--daq-port", type=int, default=int(os.environ.get("KRAKENDF_DAQ_PORT", "5000")))
    parser.add_argument("--runtime-dir", default=os.environ.get("KRAKENDF_RUNTIME"))
    return parser


def main():
    args = build_parser().parse_args()
    if args.data_dir:
        os.environ["KRAKENDF_DATA"] = args.data_dir
    os.environ["KRAKENDF_SOURCE"] = args.source
    os.environ["KRAKENDF_DAQ_MODE"] = args.daq_mode
    os.environ["KRAKENDF_DAQ_HOST"] = args.daq_host
    os.environ["KRAKENDF_DAQ_PORT"] = str(args.daq_port)
    if args.runtime_dir:
        os.environ["KRAKENDF_RUNTIME"] = args.runtime_dir
    # Tiny matrices benefit from avoiding oversized numerical thread pools.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
