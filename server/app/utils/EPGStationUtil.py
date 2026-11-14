# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from app.config import Config
from app.constants import API_REQUEST_HEADERS, JST


class EPGStationError(Exception):
    """ EPGStation の API がエラーを返したか、EPGStation に接続できなかったときの例外 """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EPGStationUtil:
    """
    録画予約のバックエンドとして EPGStation (v2 / v3) を使うためのユーティリティ
    バックエンドが Mirakurun で、かつサーバー設定の general.epgstation_url が設定されているときだけ使われる
    ref: EPGStation の /api/docs (OpenAPI)
    """

    # EPGStation の番組 ID は Mirakurun の番組 ID と同じく networkId × 10^10 + serviceId × 10^5 + eventId
    # EPGStation のチャンネル ID は networkId × 10^5 + serviceId
    PROGRAM_ID_NETWORK_FACTOR = 10 ** 10
    PROGRAM_ID_SERVICE_FACTOR = 10 ** 5


    @staticmethod
    def isEnabled() -> bool:
        """ EPGStation を録画予約のバックエンドとして使うかどうか """
        config = Config()
        return config.general.backend == 'Mirakurun' and config.general.epgstation_url is not None


    @staticmethod
    def getURL(endpoint: str) -> str:
        """ EPGStation の API の URL を返す (endpoint は /reserves のように /api 以降を渡す) """
        epgstation_url = Config().general.epgstation_url
        assert epgstation_url is not None
        return str(epgstation_url).rstrip('/') + '/api' + endpoint


    @staticmethod
    async def request(method: str, endpoint: str, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        """
        EPGStation の API を呼ぶ

        Returns:
            Any: レスポンスの JSON (本文が空のときは None)

        Raises:
            EPGStationError: 接続できなかったか、EPGStation がエラーを返した場合
        """

        try:
            async with httpx.AsyncClient(headers=API_REQUEST_HEADERS, timeout=10.0) as client:
                response = await client.request(method, EPGStationUtil.getURL(endpoint), params=params, json=json)
        except httpx.HTTPError as ex:
            raise EPGStationError(f'Failed to connect to EPGStation: {ex}') from ex

        if response.status_code >= 400:
            message = response.text
            try:
                body = response.json()
                message = body.get('message') or body.get('errors') or message
            except Exception:
                pass
            raise EPGStationError(f'EPGStation returned {response.status_code}: {message}', status_code=response.status_code)

        if len(response.content) == 0:
            return None
        try:
            return response.json()
        except ValueError:
            return None


    @staticmethod
    def toEPGStationProgramId(network_id: int, service_id: int, event_id: int) -> int:
        """ ネットワーク ID・サービス ID・イベント ID から EPGStation の番組 ID を作る """
        return (network_id * EPGStationUtil.PROGRAM_ID_NETWORK_FACTOR +
                service_id * EPGStationUtil.PROGRAM_ID_SERVICE_FACTOR + event_id)


    @staticmethod
    def parseEPGStationProgramId(program_id: int) -> tuple[int, int, int]:
        """ EPGStation の番組 ID を (ネットワーク ID, サービス ID, イベント ID) に分ける """
        network_id, rest = divmod(program_id, EPGStationUtil.PROGRAM_ID_NETWORK_FACTOR)
        service_id, event_id = divmod(rest, EPGStationUtil.PROGRAM_ID_SERVICE_FACTOR)
        return network_id, service_id, event_id


    @staticmethod
    def parseEPGStationChannelId(channel_id: int) -> tuple[int, int]:
        """ EPGStation のチャンネル ID を (ネットワーク ID, サービス ID) に分ける """
        network_id, service_id = divmod(channel_id, EPGStationUtil.PROGRAM_ID_SERVICE_FACTOR)
        return network_id, service_id


    @staticmethod
    def parseKonomiTVProgramId(program_id: str) -> tuple[int, int, int] | None:
        """ KonomiTV の番組 ID (NID32736-SID1024-EID65535) を (ネットワーク ID, サービス ID, イベント ID) に分ける """
        match = re.fullmatch(r'NID(\d+)-SID(\d+)-EID(\d+)', program_id)
        if match is None:
            return None
        return int(match.group(1)), int(match.group(2)), int(match.group(3))


    @staticmethod
    def fromUnixtimeMS(unixtime_ms: int) -> datetime:
        """ EPGStation の UnixtimeMS を JST aware な datetime にする """
        return datetime.fromtimestamp(unixtime_ms / 1000, tz=JST)
