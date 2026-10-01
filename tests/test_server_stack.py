"""Smoke test of the bounded server stack on the CI interpreter: a FastAPI app
served by Uvicorn over real loopback TCP and called with HTTPX."""

import socket
import threading
import time

import fastapi
import httpx
import uvicorn

from network_guard import register_server


def test_fastapi_under_uvicorn_answers_httpx():
    app = fastapi.FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with socket.create_server(("127.0.0.1", 0)) as sock:
        host, port = register_server(sock)
        server = uvicorn.Server(
            uvicorn.Config(app, loop="asyncio", http="h11", lifespan="off", log_level="warning")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://{host}:{port}", timeout=10) as client:
                for _ in range(100):  # wait up to about 5 s for startup
                    if server.started:
                        break
                    time.sleep(0.05)
                assert server.started
                response = client.get("/ping")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert not thread.is_alive()
