from __future__ import annotations

import unittest

from owaua import network_risk


class NetworkRiskTests(unittest.TestCase):
    def test_known_vpn_and_hosting_asns_are_restricted(self) -> None:
        self.assertTrue(network_risk.is_restricted_network(asn=39351))
        self.assertTrue(network_risk.is_restricted_network(asn=13335))
        self.assertTrue(network_risk.is_restricted_network(asn=14061))
        self.assertTrue(network_risk.is_restricted_network(asn="209242"))

    def test_residential_asns_are_not_restricted(self) -> None:
        self.assertFalse(network_risk.is_restricted_network(asn=7922))
        self.assertFalse(network_risk.is_restricted_network(asn=14593))
        self.assertFalse(network_risk.is_restricted_network(asn=0))
        self.assertFalse(network_risk.is_restricted_network(asn="not-an-asn"))

    def test_vpn_organization_names_are_restricted(self) -> None:
        self.assertTrue(network_risk.is_restricted_network(organization="Mullvad VPN AB"))
        self.assertTrue(network_risk.is_restricted_network(organization="NordVPN"))
        self.assertTrue(
            network_risk.is_restricted_network(organization="Cloudflare, Inc.")
        )
        self.assertTrue(network_risk.is_restricted_network(organization="TOR Project"))

    def test_ordinary_isp_names_are_not_restricted(self) -> None:
        self.assertFalse(network_risk.is_restricted_network(organization="Comcast Cable"))
        self.assertFalse(network_risk.is_restricted_network(organization="Starlink"))
        self.assertFalse(network_risk.is_restricted_network(organization="Deutsche Telekom AG"))
        self.assertFalse(network_risk.is_restricted_network(organization=""))
