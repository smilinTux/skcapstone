"""A forgotten transport stub fails before DNS or a production connection."""

import socket
import urllib.error
import urllib.request

import pytest


def test_external_dns_and_numeric_connections_fail_before_network_io():
    with pytest.raises(pytest.fail.Exception, match="stub its transport"):
        socket.getaddrinfo("chiap01", 18790)
    with socket.socket() as connection:
        with pytest.raises(pytest.fail.Exception, match="stub its transport"):
            connection.connect(("192.0.2.1", 18790))


def test_loopback_server_contracts_still_connect():
    with socket.socket() as server, socket.socket() as client:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client.settimeout(1)
        client.connect(server.getsockname())
        connection, _ = server.accept()
        connection.close()


@pytest.mark.parametrize(
    "url", ["https://skskills.skworld.io/api/index.json", "https://pypi.org/pypi/skcapstone/json"]
)
def test_default_public_services_are_offline_without_dns(url):
    with pytest.raises(urllib.error.URLError, match="synthetic offline service"):
        urllib.request.urlopen(url)
