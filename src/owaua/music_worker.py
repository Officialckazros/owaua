"""Disposable, credential-free metadata worker for approved media hosts. No downloads."""

import json
import re
import sys
from urllib.parse import urlparse


def main() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Hardened music lookup requires Linux resource limits")
    unrestricted = len(sys.argv) == 3 and sys.argv[2] == "--unrestricted"
    if len(sys.argv) not in {2, 3} or (len(sys.argv) == 3 and not unrestricted):
        raise ValueError("Invalid invocation")
    if not unrestricted:
        # A parent deadline also kills/reaps this process on cancellation.
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
            resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        except ImportError:
            raise RuntimeError("Media processing requires a Unix host")
    import yt_dlp
    lookup = sys.argv[1]
    parsed = urlparse(lookup)
    twitter_status = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i)/status/[0-9]{1,30}", parsed.path)
    allowed_lookup = (
        parsed.scheme == "https"
        and (
            (parsed.hostname == "www.youtube.com" and parsed.path == "/watch" and bool(parsed.query))
            or (parsed.hostname == "x.com" and twitter_status is not None and not parsed.query)
        )
    )
    if not lookup or (not unrestricted and (len(lookup) > 600 or not allowed_lookup)):
        raise ValueError("Invalid lookup")
    options = {
        # YouTube may expose the best audio stream through a protocol other
        # than the legacy http/https labels. The parent still accepts only
        # HTTPS googlevideo streams before downloading.
        "format": "bestaudio/best",
        "noplaylist": not unrestricted, "playlistend": 1,
        "quiet": True, "no_warnings": True, "cachedir": False,
        "retries": 0, "fragment_retries": 0, "socket_timeout": 8,
    }
    if unrestricted:
        options["default_search"] = "ytsearch1"
        options["js_runtimes"] = {
            "deno": {}, "node": {}, "quickjs": {}, "bun": {},
        }
    else:
        options["allowed_extractors"] = ["youtube", "twitter"]
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(lookup, download=False)
        if "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise ValueError("No track")
        result = {
            key: info.get(key)
            for key in ("duration", "is_live", "live_status", "http_headers")
        }
        for key in ("title", "url", "webpage_url", "acodec"):
            result[key] = str(info.get(key) or "")[:8192]
        print(json.dumps(result))


if __name__ == "__main__":
    main()
