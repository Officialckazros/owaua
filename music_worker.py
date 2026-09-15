"""Disposable, credential-free YouTube metadata worker. No downloads."""

import json
import sys


def main() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Hardened music lookup requires Linux resource limits")
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
    if len(lookup) > 600 or not lookup.startswith(("ytsearch1:", "https://www.youtube.com/watch?v=")):
        raise ValueError("Invalid lookup")
    options = {
        "format": "bestaudio[protocol=https]/bestaudio[protocol=http]",
        "noplaylist": True, "playlistend": 1,
        "quiet": True, "no_warnings": True, "cachedir": False,
        "retries": 0, "fragment_retries": 0, "socket_timeout": 8,
        "allowed_extractors": ["youtube", "youtube:search"],
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(lookup, download=False)
        if "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise ValueError("No track")
        result = {key: info.get(key) for key in ("duration", "is_live", "live_status")}
        for key in ("title", "url", "webpage_url", "acodec"):
            result[key] = str(info.get(key) or "")[:8192]
        print(json.dumps(result))


if __name__ == "__main__":
    main()
