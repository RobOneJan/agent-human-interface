import http.client

from agent_hub.health_server import start_health_server


def test_health_server_responds_ok() -> None:
    server = start_health_server(0)  # port 0 -> OS picks a free port
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/")
        response = conn.getresponse()
        assert response.status == 200
        assert response.read() == b'{"status": "ok"}'
    finally:
        server.shutdown()
        server.server_close()
