# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, cast

import ariblib.constants
from fastapi import APIRouter, Body

from app import logging, schemas
from app.constants import JST
from app.models.Channel import Channel
from app.models.Program import Program
from app.utils.EPGStationUtil import EPGStationError, EPGStationUtil


# 番組検索 API を EPGStation に向ける (バックエンドが Mirakurun で general.epgstation_url があるときだけ組み込まれる)
## ProgramsRouter と同じパスを持つので、app.py ではそれより先に include する (FastAPI は先に登録されたルートから照合する)
router = APIRouter(
    tags = ['Programs (EPGStation)'],
    prefix = '/api/programs',
)

# EPGStation の番組検索で返す最大件数
SEARCH_LIMIT = 3000

# EPGStation が番組に持たせるジャンルは大分類 0x0〜0xD だけ (拡張 0xE・その他 0xF は保存されない)
EPGSTATION_MAX_GENRE_LEVEL1 = 0xD

# KonomiTV のジャンル表示で「大分類のすべて」を表す中分類名
ALL_MIDDLE_GENRES = 'すべて'


def NormalizeGenreName(name: str) -> str:
    """ KonomiTV は見栄えのためにジャンル名の ／ を ・ に置き換えているので、比べるときはそろえる """
    return name.replace('／', '・')


def GetGenreTable() -> dict[int, tuple[str, dict[int, str]]]:
    """ EPGStation で検索できるジャンル (大分類 0x0〜0xD) の表を、名前を KonomiTV の表記にそろえて返す """
    table: dict[int, tuple[str, dict[int, str]]] = {}
    for major_key, (major_name, middles) in ariblib.constants.CONTENT_TYPE.items():
        if major_key > EPGSTATION_MAX_GENRE_LEVEL1:
            continue
        table[major_key] = (
            NormalizeGenreName(major_name),
            {middle_key: NormalizeGenreName(middle_name) for middle_key, middle_name in middles.items()},
        )
    return table


def EncodeGenres(genre_ranges: list[schemas.Genre], is_exclude: bool) -> list[dict[str, int]]:
    """
    KonomiTV のジャンル範囲を EPGStation の genres に変換する
    EPGStation には「除外」が無いので、除外指定のときは残りのジャンルを列挙する
    """

    table = GetGenreTable()

    # 選ばれた (大分類, 中分類) の集合を作る (中分類 None は大分類のすべて)
    selected: set[tuple[int, int | None]] = set()
    for genre in genre_ranges:
        major = NormalizeGenreName(genre['major'])
        middle = NormalizeGenreName(genre['middle'])
        for major_key, (major_name, middles) in table.items():
            if major_name != major:
                continue
            if middle == ALL_MIDDLE_GENRES:
                selected.add((major_key, None))
            else:
                for middle_key, middle_name in middles.items():
                    if middle_name == middle:
                        selected.add((major_key, middle_key))
            break

    genres: list[dict[str, int]] = []
    if is_exclude is False:
        for major_key, middle_key in sorted(selected, key=lambda item: (item[0], -1 if item[1] is None else item[1])):
            if middle_key is None:
                genres.append({'genre': major_key})
            elif (major_key, None) not in selected:
                genres.append({'genre': major_key, 'subGenre': middle_key})
        return genres

    # 除外: 大分類ごとに、除外されていない中分類を列挙する (どれも除外されていなければ大分類まるごと)
    for major_key, (_, middles) in table.items():
        if (major_key, None) in selected:
            continue
        excluded_middles = {middle for (major, middle) in selected if major == major_key and middle is not None}
        if len(excluded_middles) == 0:
            genres.append({'genre': major_key})
            continue
        for middle_key in middles:
            if middle_key not in excluded_middles:
                genres.append({'genre': major_key, 'subGenre': middle_key})
    return genres


def EncodeTimes(date_ranges: list[schemas.ProgramSearchConditionDate], is_exclude: bool) -> list[dict[str, int]]:
    """
    KonomiTV の放送日時範囲を EPGStation の times に変換する
    EPGStation は「番組の開始の曜日」と「開始の時 (0〜23)」で絞り込む。分は持てないので、
    開始は切り捨て・終了は切り上げで時単位に広げる。曜日をまたぐ範囲は日ごとに分ける (EPGStation の範囲は曜日をまたげない)
    """

    # 1 週間を 168 時間の表にして、範囲に入る時間を塗る (0 = 日曜 0 時)
    week_hours = [False] * (7 * 24)
    for date in date_ranges:
        start = date.start_day_of_week * 24 + date.start_hour
        end = date.end_day_of_week * 24 + date.end_hour + (1 if date.end_minute > 0 else 0)
        if end <= start:
            end += 7 * 24  # 週をまたぐ (土曜 → 日曜など)
        for hour in range(start, end):
            week_hours[hour % (7 * 24)] = True
    if is_exclude is True:
        week_hours = [not hour for hour in week_hours]

    # 日ごとに、続いている時間をひとまとまりにする
    times: list[dict[str, int]] = []
    for day in range(7):
        hour = 0
        while hour < 24:
            if week_hours[day * 24 + hour] is False:
                hour += 1
                continue
            start_hour = hour
            while hour < 24 and week_hours[day * 24 + hour] is True:
                hour += 1
            times.append({'week': 1 << day, 'start': start_hour, 'range': hour - start_hour})
    return times


def EncodeEPGStationSearchOption(condition: schemas.ProgramSearchCondition) -> dict[str, Any] | None:
    """
    KonomiTV の番組検索条件を EPGStation の RuleSearchOption に変換する
    検索対象が無い (チャンネル・日時が空) ときは None を返す

    EPGStation に無いので反映されないもの: あいまい検索・メモ欄・「有料のみ」(すべてとして扱う)
    """

    is_title_only = condition.is_title_only
    option: dict[str, Any] = {
        # キーワードは EDCB と同じく空白区切りの AND (EPGStation が半角にそろえて分ける)
        'keyCS': condition.is_case_sensitive,
        'keyRegExp': condition.is_regex_search_enabled,
        'name': True,
        'description': not is_title_only,
        'extended': not is_title_only,
        'ignoreKeyCS': condition.is_case_sensitive,
        'ignoreKeyRegExp': condition.is_regex_search_enabled,
        'ignoreName': True,
        'ignoreDescription': not is_title_only,
        'ignoreExtended': not is_title_only,
        'isFree': condition.broadcast_type == 'FreeOnly',
    }
    if condition.keyword.strip() != '':
        option['keyword'] = condition.keyword.strip()
    if condition.exclude_keyword.strip() != '':
        option['ignoreKeyword'] = condition.exclude_keyword.strip()

    # チャンネル: None なら全部の種別、リストならチャンネル ID (networkId × 10^5 + serviceId)
    ## EPGStation のチャンネル ID にトランスポートストリーム ID は入らないので、それは見ない
    if condition.service_ranges is None:
        option.update({'GR': True, 'BS': True, 'CS': True, 'SKY': True, 'BS4K': True})
    else:
        if len(condition.service_ranges) == 0:
            return None
        option['channelIds'] = sorted({
            service.network_id * EPGStationUtil.PROGRAM_ID_SERVICE_FACTOR + service.service_id
            for service in condition.service_ranges
        })

    if condition.genre_ranges is not None and len(condition.genre_ranges) > 0:
        genres = EncodeGenres(condition.genre_ranges, condition.is_exclude_genre_ranges)
        if len(genres) == 0:
            return None
        option['genres'] = genres

    if condition.date_ranges is not None and len(condition.date_ranges) > 0:
        times = EncodeTimes(condition.date_ranges, condition.is_exclude_date_ranges)
        if len(times) == 0:
            return None
        option['times'] = times

    # 番組長: KonomiTV は分、EPGStation は秒 (API の説明は「分」だが実装は秒 × 1000 を番組長 (ミリ秒) と比べている)
    if condition.duration_range_min is not None:
        option['durationMin'] = condition.duration_range_min * 60
    if condition.duration_range_max is not None:
        option['durationMax'] = condition.duration_range_max * 60

    return option


@router.post(
    '/search',
    summary = '番組検索 API (EPGStation)',
    response_description = '検索結果の番組情報のリスト。',
    response_model = schemas.Programs,
)
async def ProgramSearchAPI(
    program_search_condition: Annotated[schemas.ProgramSearchCondition, Body(description='番組検索条件。')],
):
    """
    EPGStation の番組検索で番組情報を検索する。番組の中身は KonomiTV のデータベース (Mirakurun から取り込んだもの) から返す。
    """

    option = EncodeEPGStationSearchOption(program_search_condition)
    if option is None:
        return schemas.Programs(total=0, programs=[])

    try:
        result = await EPGStationUtil.request('POST', '/schedules/search', json={
            'option': option,
            'isHalfWidth': True,
            'limit': SEARCH_LIMIT,
        })
    except EPGStationError as ex:
        logging.error(f'[ProgramSearchEPGStationRouter] Failed to search programs: {ex}')
        return schemas.Programs(total=0, programs=[])

    # EPGStation の番組 ID を KonomiTV の番組 ID (NID…-SID…-EID…) にする
    program_ids: list[str] = []
    for item in cast(list[dict[str, Any]], result or []):
        network_id, service_id, event_id = EPGStationUtil.parseEPGStationProgramId(item['id'])
        program_ids.append(f'NID{network_id}-SID{service_id:03d}-EID{event_id}')
    if len(program_ids) == 0:
        return schemas.Programs(total=0, programs=[])

    # KonomiTV で視聴できるチャンネルの、放送中・これからの番組だけを返す
    watchable_channel_ids = set(await Channel.filter(is_watchable=True).values_list('id', flat=True))
    now = datetime.now(JST)
    programs_by_id = {program.id: program for program in await Program.filter(id__in=program_ids)}
    programs: list[Program] = []
    for program_id in program_ids:
        program = programs_by_id.get(program_id)
        if program is None or program.channel_id not in watchable_channel_ids:
            continue
        if program.end_time <= now:
            continue
        programs.append(program)
    programs.sort(key=lambda program: program.start_time)

    return schemas.Programs(total=len(programs), programs=cast(Any, programs))
