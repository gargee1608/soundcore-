"""Input-to-output relay. The same PCM buffer is recorded and played."""

from __future__ import annotations


class Router:
    def __init__(self):
        self._routes: dict[int, list] = {}

    def set_routes(self, routes: dict[int, list]) -> None:
        self._routes = routes

    def playbacks_for(self, input_channel_id: int) -> list:
        return list(self._routes.get(input_channel_id, []))
