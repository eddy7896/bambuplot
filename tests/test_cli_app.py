import socket

from bambuplot.cli import _bambuplot_at, _port_free


def test_busy_port_is_detected_and_nothing_is_mistaken_for_bambuplot():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert not _port_free("127.0.0.1", port)
        assert not _bambuplot_at("127.0.0.1", port, timeout=0.2)  # listening, but not BambuPlot
