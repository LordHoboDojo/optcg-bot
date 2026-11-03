"""Run the hosted site.

    .venv/bin/python -m optcg.server                     # http://127.0.0.1:8000, settings from the environment
    OPTCG_DEV_LOGIN=1 .venv/bin/python -m optcg.server   # sign in without Google, for trying it locally
    .venv/bin/python -m optcg.server --art-manifest art.json  # list StreamingAssets' art, for art kept on a CDN
"""
import argparse
import json
import os

import uvicorn

from .app import create_app
from .config import Config
from .logs import setup


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--art-manifest", metavar="FILE", help="write the list of art files in StreamingAssets and exit")
    args = ap.parse_args(argv)
    if args.art_manifest:
        from ..replay import art_files
        files = art_files()
        if not files:
            raise SystemExit("no art in StreamingAssets/")
        with open(args.art_manifest, "w") as f:
            json.dump(files, f)
        return print(f"{len(files)} files -> {args.art_manifest}")
    setup()
    uvicorn.run(create_app(Config.from_env()), host=args.host, port=args.port, log_config=None, access_log=False,
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
