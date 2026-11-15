# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, status

from app import logging, schemas
from app.models.Channel import Channel
from app.routers.ProgramSearchEPGStationRouter import (
    ALL_MIDDLE_GENRES,
    EncodeEPGStationSearchOption,
    GetGenreTable,
)
from app.utils.EPGStationUtil import EPGStationError, EPGStationUtil


# キーワード自動予約条件の API を EPGStation のルールに向ける (バックエンドが Mirakurun で general.epgstation_url があるときだけ組み込まれる)
## ReservationConditionsRouter と同じパスを持つので、app.py ではそれより先に include する
router = APIRouter(
    tags = ['Reservation Conditions (EPGStation)'],
    prefix = '/api/recording/conditions',
)

# EPGStation のチャンネル種別 (ルールでチャンネルを指定しないときに使う)
CHANNEL_TYPES = ('GR', 'BS', 'CS', 'SKY', 'BS4K')


def RaiseEPGStationError(ex: EPGStationError, action: str) -> None:
    logging.error(f'[ReservationConditionsEPGStationRouter] Failed to {action}: {ex}')
    raise HTTPException(
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail = f'Failed to {action} on EPGStation: {ex}',
    )


async def DecodeEPGStationRule(rule: dict[str, Any], channels: list[Channel], reserve_counts: dict[int, int]) -> schemas.ReservationCondition:
    """ EPGStation のルールを schemas.ReservationCondition に変換する """

    search: dict[str, Any] = rule.get('searchOption', {})
    reserve: dict[str, Any] = rule.get('reserveOption', {})

    # チャンネル: channelIds があればそのチャンネル、無ければチャンネル種別 (全種別なら None = 全チャンネル)
    service_ranges: list[schemas.ProgramSearchConditionService] | None = None
    channel_ids: list[int] = search.get('channelIds') or []
    if len(channel_ids) > 0:
        service_ranges = []
        for channel_id in channel_ids:
            network_id, service_id = EPGStationUtil.parseEPGStationChannelId(channel_id)
            channel = next((c for c in channels if c.network_id == network_id and c.service_id == service_id), None)
            service_ranges.append(schemas.ProgramSearchConditionService(
                network_id = network_id,
                transport_stream_id = channel.transport_stream_id or 0 if channel is not None else 0,
                service_id = service_id,
            ))
    else:
        channel_types = [channel_type for channel_type in CHANNEL_TYPES if search.get(channel_type) is True]
        if not all(search.get(channel_type) is True for channel_type in ('GR', 'BS', 'CS')):
            service_ranges = [
                schemas.ProgramSearchConditionService(
                    network_id = c.network_id,
                    transport_stream_id = c.transport_stream_id or 0,
                    service_id = c.service_id,
                )
                for c in channels if c.type in channel_types
            ]

    # ジャンル: 中分類が無ければ「すべて」
    genre_ranges: list[schemas.Genre] | None = None
    table = GetGenreTable()
    for genre in search.get('genres') or []:
        if genre.get('genre') not in table:
            continue
        major_name, middles = table[genre['genre']]
        middle_name = ALL_MIDDLE_GENRES if genre.get('subGenre') is None else middles.get(genre['subGenre'], '未定義')
        if genre_ranges is None:
            genre_ranges = []
        genre_ranges.append({'major': major_name, 'middle': middle_name})

    # 曜日と時刻: 曜日のビットごとに 1 つの範囲にする (時刻の指定が無ければその曜日の終日)
    date_ranges: list[schemas.ProgramSearchConditionDate] | None = None
    for time in search.get('times') or []:
        start = time.get('start')
        time_range = time.get('range')
        for day in range(7):
            if (time.get('week', 0) >> day) & 1 == 0:
                continue
            if date_ranges is None:
                date_ranges = []
            if start is None or time_range is None:
                date_ranges.append(schemas.ProgramSearchConditionDate(
                    start_day_of_week=day, start_hour=0, start_minute=0, end_day_of_week=day, end_hour=23, end_minute=59))
                continue
            end = start + time_range
            if end >= 24:
                date_ranges.append(schemas.ProgramSearchConditionDate(
                    start_day_of_week=day, start_hour=start, start_minute=0,
                    end_day_of_week=day, end_hour=23, end_minute=59) if end == 24 else schemas.ProgramSearchConditionDate(
                    start_day_of_week=day, start_hour=start, start_minute=0,
                    end_day_of_week=(day + 1) % 7, end_hour=end - 24, end_minute=0))
            else:
                date_ranges.append(schemas.ProgramSearchConditionDate(
                    start_day_of_week=day, start_hour=start, start_minute=0, end_day_of_week=day, end_hour=end, end_minute=0))

    is_title_only = search.get('keyword') is not None and search.get('name') is True and not search.get('description') and not search.get('extended')
    is_enabled = reserve.get('enable', True) is True

    program_search_condition = schemas.ProgramSearchCondition(
        is_enabled = is_enabled,
        keyword = search.get('keyword') or '',
        exclude_keyword = search.get('ignoreKeyword') or '',
        is_title_only = is_title_only,
        is_case_sensitive = search.get('keyCS') is True,
        is_regex_search_enabled = search.get('keyRegExp') is True,
        service_ranges = service_ranges,
        genre_ranges = genre_ranges,
        date_ranges = date_ranges,
        # EPGStation の番組長は秒、KonomiTV は分
        duration_range_min = search['durationMin'] // 60 if search.get('durationMin') is not None else None,
        duration_range_max = search['durationMax'] // 60 if search.get('durationMax') is not None else None,
        broadcast_type = 'FreeOnly' if search.get('isFree') is True else 'All',
        # EPGStation の「録画済みの重複番組を排除する」はチャンネルを区別しない
        duplicate_title_check_scope = 'AllChannels' if reserve.get('avoidDuplicate') is True else 'None',
        duplicate_title_check_period_days = reserve.get('periodToAvoidDuplicate') or 6,
    )

    return schemas.ReservationCondition(
        id = rule['id'],
        reservation_count = reserve_counts.get(rule['id'], 0),
        program_search_condition = program_search_condition,
        record_settings = schemas.RecordSettings(is_enabled=is_enabled),
    )


def EncodeEPGStationRule(
    program_search_condition: schemas.ProgramSearchCondition,
    record_settings: schemas.RecordSettings,
    current_rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    KonomiTV の検索条件と録画設定を EPGStation の AddRuleOption にする
    更新のときは、KonomiTV では扱わない EPGStation の設定 (保存先・エンコード・タグ・末尾切れ) を今のルールから引き継ぐ
    """

    search_option = EncodeEPGStationSearchOption(program_search_condition)
    if search_option is None:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'The search condition has no target (channels, genres or dates are empty)',
        )

    current_reserve: dict[str, Any] = (current_rule or {}).get('reserveOption', {})
    reserve_option: dict[str, Any] = {
        'enable': program_search_condition.is_enabled and record_settings.is_enabled,
        'allowEndLack': current_reserve.get('allowEndLack', True),
        'avoidDuplicate': program_search_condition.duplicate_title_check_scope != 'None',
    }
    if reserve_option['avoidDuplicate'] is True:
        reserve_option['periodToAvoidDuplicate'] = program_search_condition.duplicate_title_check_period_days
    if current_reserve.get('tags'):
        reserve_option['tags'] = current_reserve['tags']

    rule: dict[str, Any] = {
        'isTimeSpecification': False,
        'searchOption': search_option,
        'reserveOption': reserve_option,
    }
    for key in ('saveOption', 'encodeOption'):
        if current_rule is not None and current_rule.get(key) is not None:
            rule[key] = current_rule[key]
    return rule


async def CountReservesByRule() -> dict[int, int]:
    """ ルールごとの予約の数 (EPGStation の reservesCnt は 0 のままのことがあるので、予約一覧から数える) """
    try:
        result = await EPGStationUtil.request('GET', '/reserves', params={'type': 'all', 'isHalfWidth': 'true', 'limit': 10000})
    except EPGStationError:
        return {}
    counts: dict[int, int] = {}
    for reserve in cast(list[dict[str, Any]], (result or {}).get('reserves', [])):
        if reserve.get('ruleId') is not None and not reserve.get('isSkip'):
            counts[reserve['ruleId']] = counts.get(reserve['ruleId'], 0) + 1
    return counts


async def FetchRule(reservation_condition_id: int) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], await EPGStationUtil.request('GET', f'/rules/{reservation_condition_id}'))
    except EPGStationError as ex:
        if ex.status_code == 404:
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified reservation_condition_id was not found',
            )
        RaiseEPGStationError(ex, 'get the reserve condition')
        raise


@router.get(
    '',
    summary = 'キーワード自動予約条件一覧 API (EPGStation)',
    response_description = 'キーワード自動予約条件のリスト。',
    response_model = schemas.ReservationConditions,
)
async def ReservationConditionsAPI():
    """
    EPGStation のすべてのルール (時刻指定のルールを除く) をキーワード自動予約条件として返す。
    """

    try:
        result = await EPGStationUtil.request('GET', '/rules', params={'limit': 10000})
    except EPGStationError as ex:
        logging.error(f'[ReservationConditionsEPGStationRouter] Failed to get rules: {ex}')
        return schemas.ReservationConditions(total=0, reservation_conditions=[])

    channels = await Channel.filter(is_watchable=True)
    reserve_counts = await CountReservesByRule()
    conditions = [
        await DecodeEPGStationRule(rule, channels, reserve_counts)
        for rule in cast(list[dict[str, Any]], (result or {}).get('rules', []))
        if rule.get('isTimeSpecification') is not True
    ]
    return schemas.ReservationConditions(total=len(conditions), reservation_conditions=conditions)


@router.post(
    '',
    summary = 'キーワード自動予約条件登録 API (EPGStation)',
    status_code = status.HTTP_201_CREATED,
)
async def RegisterReservationConditionAPI(
    reserve_condition_add_request: Annotated[schemas.ReservationConditionAddRequest, Body(description='登録するキーワード自動予約条件。')],
):
    """
    EPGStation にルールを追加する。
    """

    rule = EncodeEPGStationRule(reserve_condition_add_request.program_search_condition, reserve_condition_add_request.record_settings)
    try:
        result = await EPGStationUtil.request('POST', '/rules', json=rule)
    except EPGStationError as ex:
        RaiseEPGStationError(ex, 'register the reserve condition')
        raise
    logging.info(f'[ReservationConditionsEPGStationRouter] Registered a rule. [rule_id: {(result or {}).get("ruleId")}]')


@router.get(
    '/{reservation_condition_id}',
    summary = 'キーワード自動予約条件取得 API (EPGStation)',
    response_description = 'キーワード自動予約条件。',
    response_model = schemas.ReservationCondition,
)
async def ReservationConditionAPI(
    reservation_condition_id: Annotated[int, Path(description='キーワード自動予約条件 ID (EPGStation のルール ID) 。')],
):
    rule = await FetchRule(reservation_condition_id)
    return await DecodeEPGStationRule(rule, await Channel.filter(is_watchable=True), await CountReservesByRule())


@router.put(
    '/{reservation_condition_id}',
    summary = 'キーワード自動予約条件更新 API (EPGStation)',
    response_description = '更新されたキーワード自動予約条件。',
    response_model = schemas.ReservationCondition,
)
async def UpdateReservationConditionAPI(
    reservation_condition_id: Annotated[int, Path(description='キーワード自動予約条件 ID (EPGStation のルール ID) 。')],
    reserve_condition_update_request: Annotated[schemas.ReservationConditionUpdateRequest, Body(description='更新するキーワード自動予約条件。')],
):
    current_rule = await FetchRule(reservation_condition_id)
    rule = EncodeEPGStationRule(
        reserve_condition_update_request.program_search_condition,
        reserve_condition_update_request.record_settings,
        current_rule,
    )
    try:
        await EPGStationUtil.request('PUT', f'/rules/{reservation_condition_id}', json=rule)
    except EPGStationError as ex:
        RaiseEPGStationError(ex, 'update the reserve condition')
    return await DecodeEPGStationRule(
        await FetchRule(reservation_condition_id), await Channel.filter(is_watchable=True), await CountReservesByRule())


@router.delete(
    '/{reservation_condition_id}',
    summary = 'キーワード自動予約条件削除 API (EPGStation)',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def DeleteReservationConditionAPI(
    reservation_condition_id: Annotated[int, Path(description='キーワード自動予約条件 ID (EPGStation のルール ID) 。')],
):
    """
    EPGStation のルールを削除する (EPGStation がそのルールの予約も消す)。
    """

    await FetchRule(reservation_condition_id)
    try:
        await EPGStationUtil.request('DELETE', f'/rules/{reservation_condition_id}')
    except EPGStationError as ex:
        RaiseEPGStationError(ex, 'delete the reserve condition')
