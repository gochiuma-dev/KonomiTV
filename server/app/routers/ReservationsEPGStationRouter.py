# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Body, HTTPException, Path, status

from app import logging, schemas
from app.models.Channel import Channel
from app.models.Program import Program
from app.utils.EPGStationUtil import EPGStationError, EPGStationUtil
from app.utils.TSInformation import TSInformation


# 録画予約まわりの API を EPGStation に向ける (バックエンドが Mirakurun で general.epgstation_url があるときだけ組み込まれる)
## ReservationsRouter・RecordingPresetsRouter と同じパスを持つので、app.py ではそれらより先に include する
## (FastAPI は先に登録されたルートから照合する)
router = APIRouter(
    tags = ['Reservations (EPGStation)'],
    prefix = '/api/recording',
)

# 想定録画ファイルサイズの計算に使うビットレート (kbps)
## EPGStation はビットレートの情報を持たないので、放送の種類ごとのおおよその値を使う
ESTIMATED_BITRATE_KBPS: dict[str, int] = {
    'GR': 17000,
    'BS': 24000,
    'CS': 13000,
}


def RaiseEPGStationError(ex: EPGStationError, action: str) -> None:
    """ EPGStation のエラーを HTTPException にして投げる """
    logging.error(f'[ReservationsEPGStationRouter] Failed to {action}: {ex}')
    raise HTTPException(
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail = f'Failed to {action} on EPGStation: {ex}',
    )


async def FetchReserveItems() -> list[dict[str, Any]]:
    """ EPGStation からすべての録画予約 (除外・重複・競合を含む) を取得する """
    result = await EPGStationUtil.request('GET', '/reserves', params={'type': 'all', 'isHalfWidth': 'false', 'limit': 10000})
    return cast(list[dict[str, Any]], (result or {}).get('reserves', []))


async def FetchReserveItem(reservation_id: int) -> dict[str, Any]:
    """ EPGStation から指定された録画予約を取得する (無ければ 422) """
    try:
        return cast(dict[str, Any], await EPGStationUtil.request('GET', f'/reserves/{reservation_id}', params={'isHalfWidth': 'false'}))
    except EPGStationError as ex:
        if ex.status_code == 404:
            logging.error(f'[ReservationsEPGStationRouter] Specified reservation_id was not found. [reservation_id: {reservation_id}]')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified reservation_id was not found',
            )
        RaiseEPGStationError(ex, 'get the recording reservation')
        raise


async def FetchRecordingKeys() -> set[tuple[int, int]]:
    """ EPGStation で録画中の番組を (チャンネル ID, 開始時刻) の集合で返す (録画中の情報に予約 ID が無いため) """
    try:
        result = await EPGStationUtil.request('GET', '/recording', params={'isHalfWidth': 'true', 'limit': 100})
    except EPGStationError as ex:
        logging.warning(f'[ReservationsEPGStationRouter] Failed to get recording programs: {ex}')
        return set()
    keys: set[tuple[int, int]] = set()
    for record in (result or {}).get('records', []):
        if record.get('isRecording') is True and record.get('channelId') is not None:
            keys.add((record['channelId'], record['startAt']))
    return keys


def IsReserveRecording(reserve_item: dict[str, Any], recording_keys: set[tuple[int, int]]) -> bool:
    return (reserve_item['channelId'], reserve_item['startAt']) in recording_keys


def GetReserveEventTriplet(reserve_item: dict[str, Any]) -> tuple[int, int, int]:
    """ 録画予約の (ネットワーク ID, サービス ID, イベント ID) を返す (時刻指定予約のイベント ID は 0) """
    network_id, service_id = EPGStationUtil.parseEPGStationChannelId(reserve_item['channelId'])
    event_id = 0
    if reserve_item.get('programId') is not None:
        _, _, event_id = EPGStationUtil.parseEPGStationProgramId(reserve_item['programId'])
    return network_id, service_id, event_id


async def BuildFallbackChannel(network_id: int, service_id: int) -> Channel:
    """ KonomiTV のデータベースに無いチャンネルの、間に合わせのチャンネル情報を作る (ReservationsRouter と同じ作り方) """
    channel = Channel(
        id = f'NID{network_id}-SID{service_id}',
        display_channel_id = 'gr001',
        network_id = network_id,
        service_id = service_id,
        transport_stream_id = None,
        remocon_id = 0,
        channel_number = '001',
        type = TSInformation.getNetworkType(network_id),
        name = f'NID{network_id}-SID{service_id}',
        jikkyo_force = False,
        is_subchannel = False,
        is_radiochannel = False,
        is_watchable = False,
    )
    if channel.type != 'GR':
        channel.remocon_id = TSInformation.calculateRemoconID(channel.type, channel.service_id)
    channel.channel_number = await TSInformation.calculateChannelNumber(
        channel.type, channel.network_id, channel.service_id, channel.remocon_id,
    )
    channel.display_channel_id = channel.type.lower() + channel.channel_number
    channel.is_subchannel = TSInformation.calculateIsSubchannel(channel.type, channel.service_id)
    return channel


async def DecodeEPGStationReserveItem(
    reserve_item: dict[str, Any],
    channels: list[Channel],
    recording_keys: set[tuple[int, int]],
) -> schemas.Reservation:
    """ EPGStation の ReserveItem を schemas.Reservation に変換する """

    network_id, service_id, event_id = GetReserveEventTriplet(reserve_item)

    channel = next((c for c in channels if c.network_id == network_id and c.service_id == service_id), None)
    if channel is None:
        channel = await BuildFallbackChannel(network_id, service_id)

    title = TSInformation.formatString(reserve_item.get('name', ''))
    start_time: datetime = EPGStationUtil.fromUnixtimeMS(reserve_item['startAt'])
    end_time: datetime = EPGStationUtil.fromUnixtimeMS(reserve_item['endAt'])
    duration = (end_time - start_time).total_seconds()

    program: Program | None = None
    if event_id != 0:
        program = await Program.filter(network_id=network_id, service_id=service_id, event_id=event_id).get_or_none()
    if program is None:
        program = Program(
            id = f'NID{network_id}-SID{service_id:03d}-EID{event_id}',
            channel_id = channel.id,
            network_id = network_id,
            service_id = service_id,
            event_id = event_id,
            title = title,
            description = TSInformation.formatString(reserve_item.get('description', '') or ''),
            detail = {},
            start_time = start_time,
            end_time = end_time,
            duration = duration,
            is_free = True,
            genres = [],
            video_type = '映像1080i(1125i)、アスペクト比16:9 パンベクトルなし',
            video_codec = 'mpeg2',
            video_resolution = '1080i',
            primary_audio_type = '1/0モード(シングルモノ)',
            primary_audio_language = '日本語',
            primary_audio_sampling_rate = '48kHz',
            secondary_audio_type = None,
            secondary_audio_language = None,
            secondary_audio_sampling_rate = None,
        )
    else:
        # 延長などで EPGStation の予約の方が新しい可能性があるので、時刻は予約の値で上書きする
        program.start_time = start_time
        program.end_time = end_time
        program.duration = duration

    # 競合 (チューナー不足) なら録画できない
    recording_availability: Literal['Full', 'Partial', 'Unavailable'] = 'Unavailable' if reserve_item.get('isConflict') else 'Full'

    # ルール (キーワード自動予約) から入った予約は EDCB と同じく「EPG自動予約」とする
    comment = 'EPG自動予約' if reserve_item.get('ruleId') is not None else ''

    # 除外・重複された予約は無効として見せる
    record_settings = schemas.RecordSettings(
        is_enabled = not (reserve_item.get('isSkip') or reserve_item.get('isOverlap')),
    )

    bitrate_kbps = ESTIMATED_BITRATE_KBPS.get(channel.type, 17000)
    estimated_recording_file_size = int(duration * bitrate_kbps * 1000 / 8)

    return schemas.Reservation(
        id = reserve_item['id'],
        channel = cast(Any, channel),
        program = cast(Any, program),
        is_recording_in_progress = IsReserveRecording(reserve_item, recording_keys),
        recording_availability = recording_availability,
        comment = comment,
        scheduled_recording_file_name = '',
        estimated_recording_file_size = estimated_recording_file_size,
        record_settings = record_settings,
    )


async def GetTimeTableReservations(
    channels_result: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """
    番組表 API 向けに、EPGStation の録画予約を番組 ID ごと・チャンネルごとにまとめて返す
    (ProgramsRouter.TimeTableAPI の EDCB 向けの処理と同じ形)
    """

    reservations_by_program_id: dict[str, dict[str, Any]] = {}
    reservations_by_channel_time: dict[str, list[dict[str, Any]]] = {}

    channel_id_by_service: dict[tuple[int, int], str] = {
        (row['network_id'], row['service_id']): row['id'] for row in channels_result
    }

    reserve_items = await FetchReserveItems()
    recording_keys = await FetchRecordingKeys()

    for reserve_item in reserve_items:
        network_id, service_id, event_id = GetReserveEventTriplet(reserve_item)
        start_time = EPGStationUtil.fromUnixtimeMS(reserve_item['startAt'])
        end_time = EPGStationUtil.fromUnixtimeMS(reserve_item['endAt'])

        reservation_status: Literal['Reserved', 'Recording', 'Disabled']
        if reserve_item.get('isSkip') or reserve_item.get('isOverlap'):
            reservation_status = 'Disabled'
        elif IsReserveRecording(reserve_item, recording_keys):
            reservation_status = 'Recording'
        else:
            reservation_status = 'Reserved'

        reservation = {
            'id': reserve_item['id'],
            'status': reservation_status,
            'recording_availability': 'Unavailable' if reserve_item.get('isConflict') else 'Full',
        }
        if event_id != 0:
            reservations_by_program_id[f'NID{network_id}-SID{service_id:03d}-EID{event_id}'] = reservation

        channel_id = channel_id_by_service.get((network_id, service_id))
        if channel_id is not None:
            reservations_by_channel_time.setdefault(channel_id, []).append({
                'start_time': start_time,
                'end_time': end_time,
                'reservation': reservation,
            })

    return reservations_by_program_id, reservations_by_channel_time


@router.get(
    '/reservations',
    summary = '録画予約情報一覧 API (EPGStation)',
    response_description = '録画予約の情報のリスト。',
    response_model = schemas.Reservations,
)
async def ReservationsAPI():
    """
    EPGStation のすべての録画予約の情報を取得する。
    """

    try:
        reserve_items = await FetchReserveItems()
    except EPGStationError as ex:
        logging.error(f'[ReservationsEPGStationRouter] Failed to get the list of recording reservations: {ex}')
        return schemas.Reservations(total=0, reservations=[])
    recording_keys = await FetchRecordingKeys()
    channels = await Channel.all()

    reservations = [await DecodeEPGStationReserveItem(item, channels, recording_keys) for item in reserve_items]
    reservations.sort(key=lambda reservation: reservation.program.start_time)

    return schemas.Reservations(total=len(reservations), reservations=reservations)


@router.post(
    '/reservations',
    summary = '録画予約追加 API (EPGStation)',
    status_code = status.HTTP_201_CREATED,
)
async def AddReservationAPI(
    reserve_add_request: Annotated[schemas.ReservationAddRequest, Body(description='追加する録画予約の設定。')],
):
    """
    EPGStation に番組の録画予約を追加する。<br>
    EPGStation には予約ごとの録画マージンや優先度が無いので、record_settings のうち反映されるものは無い。
    """

    triplet = EPGStationUtil.parseKonomiTVProgramId(reserve_add_request.program_id)
    if triplet is None:
        logging.error(f'[ReservationsEPGStationRouter] Invalid program_id. [program_id: {reserve_add_request.program_id}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified program was not found',
        )
    epgstation_program_id = EPGStationUtil.toEPGStationProgramId(*triplet)

    try:
        reserve_items = await FetchReserveItems()
    except EPGStationError as ex:
        RaiseEPGStationError(ex, 'get the list of recording reservations')
        raise
    if any(item.get('programId') == epgstation_program_id for item in reserve_items):
        logging.error(f'[ReservationsEPGStationRouter] The same program_id is already reserved. [program_id: {reserve_add_request.program_id}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'The same program_id is already reserved',
        )

    try:
        result = await EPGStationUtil.request('POST', '/reserves', json={
            'programId': epgstation_program_id,
            'allowEndLack': False,
        })
    except EPGStationError as ex:
        RaiseEPGStationError(ex, 'add a recording reservation')
        raise

    logging.info(f'[ReservationsEPGStationRouter] Added a recording reservation. [program_id: {reserve_add_request.program_id} / reserve_id: {(result or {}).get("reserveId")}]')


@router.get(
    '/reservations/{reservation_id}',
    summary = '録画予約情報取得 API (EPGStation)',
    response_description = '録画予約の情報。',
    response_model = schemas.Reservation,
)
async def ReservationAPI(
    reservation_id: Annotated[int, Path(description='録画予約 ID 。')],
):
    """
    EPGStation の指定された録画予約の情報を取得する。
    """

    reserve_item = await FetchReserveItem(reservation_id)
    return await DecodeEPGStationReserveItem(reserve_item, await Channel.all(), await FetchRecordingKeys())


@router.put(
    '/reservations/{reservation_id}',
    summary = '録画予約設定更新 API (EPGStation)',
    response_description = '更新された録画予約の情報。',
    response_model = schemas.Reservation,
)
async def UpdateReservationAPI(
    reservation_id: Annotated[int, Path(description='録画予約 ID 。')],
    reserve_update_request: Annotated[schemas.ReservationUpdateRequest, Body(description='更新する録画予約の設定。')],
):
    """
    EPGStation の指定された録画予約の設定を更新する。<br>
    反映されるのは有効/無効だけ。ルール (キーワード自動予約) の予約は無効にすると「除外」、有効にすると除外の解除になる。<br>
    手動予約は EPGStation では無効にできないので 422 を返す (削除する)。
    """

    reserve_item = await FetchReserveItem(reservation_id)
    is_enabled_now = not (reserve_item.get('isSkip') or reserve_item.get('isOverlap'))
    is_enabled_requested = reserve_update_request.record_settings.is_enabled

    try:
        if is_enabled_requested is False and is_enabled_now is True:
            if reserve_item.get('ruleId') is None:
                raise HTTPException(
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail = 'Manual reservations cannot be disabled on EPGStation. Delete it instead',
                )
            # ルール予約の削除は EPGStation では「除外」になる
            await EPGStationUtil.request('DELETE', f'/reserves/{reservation_id}')
        elif is_enabled_requested is True and is_enabled_now is False:
            if reserve_item.get('isSkip'):
                await EPGStationUtil.request('DELETE', f'/reserves/{reservation_id}/skip')
            if reserve_item.get('isOverlap'):
                await EPGStationUtil.request('DELETE', f'/reserves/{reservation_id}/overlap')
    except EPGStationError as ex:
        RaiseEPGStationError(ex, 'update the recording reservation')
        raise

    updated_reserve_item = await FetchReserveItem(reservation_id)
    return await DecodeEPGStationReserveItem(updated_reserve_item, await Channel.all(), await FetchRecordingKeys())


@router.delete(
    '/reservations/{reservation_id}',
    summary = '録画予約削除 API (EPGStation)',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def DeleteReservationAPI(
    reservation_id: Annotated[int, Path(description='録画予約 ID 。')],
):
    """
    EPGStation の指定された録画予約を削除する (ルールの予約は EPGStation の仕様で「除外」になる)。
    """

    await FetchReserveItem(reservation_id)
    try:
        await EPGStationUtil.request('DELETE', f'/reserves/{reservation_id}')
    except EPGStationError as ex:
        RaiseEPGStationError(ex, 'delete the recording reservation')


@router.get(
    '/presets',
    summary = '録画設定プリセット一覧 API (EPGStation)',
    response_description = 'グローバルデフォルト値と録画設定プリセットの一覧。',
    response_model = schemas.RecordSettingsPresets,
)
async def RecordingPresetsAPI() -> schemas.RecordSettingsPresets:
    """
    EPGStation には録画設定プリセットが無いので、デフォルトのプリセット 1 つだけを返す。
    """

    return schemas.RecordSettingsPresets(
        global_defaults = schemas.RecordSettingsGlobalDefaults(
            # EPGStation は番組の開始・終了に合わせて録るので、録画マージンは 0 として見せる
            recording_start_margin = 0,
            recording_end_margin = 0,
            caption_recording_mode = 'Enable',
            data_broadcasting_recording_mode = 'Enable',
            post_recording_mode = 'Nothing',
        ),
        presets = [
            schemas.RecordSettingsPreset(id=0, name='デフォルト', record_settings=schemas.RecordSettings()),
        ],
    )

