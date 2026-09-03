"""Local classification of VPN, proxy, Tor, and hosting networks.

Cloudflare already knows the visitor ASN and organization name. owaua uses
that metadata in memory during ToS acceptance and does not store it. This is
a block-evasion control, not identity.
"""

from __future__ import annotations

import re
from typing import Final

# Hosting, cloud, VPN-provider, and overlay ASNs. Residential ISPs are omitted.
# Cloudflare WARP is 13335 / 209242; Mullvad is 39351; Proton is 62371 / 209103.
RESTRICTED_ASNS: Final = frozenset(
    {
        13335,
        14061,
        14618,
        15169,
        16276,
        16509,
        20473,
        209103,
        209242,
        209854,
        212238,
        213230,
        24940,
        31898,
        33387,
        35540,
        36352,
        39351,
        394380,
        396507,
        399629,
        40676,
        45102,
        46475,
        46562,
        46844,
        51167,
        53667,
        58065,
        60068,
        60781,
        62240,
        62371,
        63949,
        8075,
        8100,
        8560,
        9009,
        12876,
        132203,
        19148,
        197540,
        198187,
        19994,
        202053,
        206170,
        20860,
        208323,
        21769,
        28753,
        29802,
        30633,
        33070,
        35913,
        35916,
        49981,
        51159,
        51852,
        55286,
        59253,
        62567,
        62838,
        63018,
        7203,
        141995,
        18779,
        200019,
        202425,
        206057,
        206804,
        212317,
        32181,
        32780,
        49453,
        203959,
    }
)

_ORG_RE = re.compile(
    r"(?i)(?:"
    r"\bvpn\b|anonymizer|\bprox(?:y|ies)\b|datacent(?:er|re)|data[\s-]?cent(?:er|re)|"
    r"\bhosting\b|\bvps\b|\bcolo(?:cation)?\b|dedicated servers?|"
    r"nord\s*vpn|mullvad|protonvpn|proton ag|surfshark|expressvpn|windscribe|"
    r"cyberghost|ipvanish|tunnelbear|vypr\s*vpn|hidemyass|\bhma\b|"
    r"private internet access|\bpackethub\b|\bm247\b|\bdatacamp\b|"
    r"digitalocean|\blinode\b|\bvultr\b|hetzner|\bovh\b|contabo|leaseweb|"
    r"scaleway|psychz|sharktech|buyvm|frantech|clouvider|choopa|hostinger|"
    r"cloudflare|\bwarp\b|private relay|torservers|tor[\s-]exit|\btor\b|"
    r"quadranet|colocrossing|limestone networks"
    r")"
)


def parse_asn(value: object) -> int:
    raw = str(value or "").strip()
    if not raw.isdigit() or len(raw) > 10:
        return 0
    asn = int(raw)
    if not 1 <= asn <= 4_294_967_294:
        return 0
    return asn


def parse_organization(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 120 or any(ord(char) < 32 or ord(char) > 126 for char in raw):
        return ""
    return raw


def is_restricted_network(*, asn: object = 0, organization: object = "") -> bool:
    """True when Cloudflare metadata indicates VPN, proxy, Tor, or hosting."""
    parsed_asn = parse_asn(asn)
    if parsed_asn in RESTRICTED_ASNS:
        return True
    org = parse_organization(organization)
    return bool(org and _ORG_RE.search(org))
