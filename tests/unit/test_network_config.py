import unittest

from browser_manager.network_config import normalize_network_config, proxy_launch_args


class NetworkConfigTests(unittest.TestCase):
    def test_direct_route_is_explicit_and_has_no_fallback(self):
        self.assertEqual(normalize_network_config(None), {"mode": "direct"})
        self.assertEqual(proxy_launch_args({"mode": "direct"}), ["--no-proxy-server"])

    def test_fixed_route_is_normalized(self):
        config = normalize_network_config({
            "mode": "fixed",
            "scheme": "https",
            "host": "Proxy.Example.com",
            "port": 443,
            "authentication": "none",
        })
        self.assertEqual(config["host"], "proxy.example.com")
        self.assertEqual(proxy_launch_args(config), ["--proxy-server=https://proxy.example.com:443"])

    def test_socks5_basic_auth_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_network_config({
                "mode": "fixed",
                "scheme": "socks5",
                "host": "127.0.0.1",
                "port": 1080,
                "authentication": "basic",
            })


if __name__ == "__main__":
    unittest.main()
